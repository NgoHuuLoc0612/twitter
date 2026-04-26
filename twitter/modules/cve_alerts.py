"""
CVEAlerts — Security CVE tracking via NIST National Vulnerability Database (NVD).

NVD API v2.0
------------
- Endpoint: https://services.nvd.nist.gov/rest/json/cves/2.0
- Rate limits: 5 req/30s (no key) | 50 req/30s (with API key)
- Docs: https://nvd.nist.gov/developers/vulnerabilities

Features
--------
* Fetch recently published / modified CVEs (configurable time window)
* Filter by CVSS score, severity (Critical/High/Medium/Low), CWE, CPE/product
* Keyword search within descriptions
* Paginated batch fetching for large result sets
* Cache all CVEs in SQLite with dedup
* Auto-tweet untweeted CVEs above configurable CVSS threshold
* Configurable tweet template with severity emoji mapping
* Supports NVD API key for higher rate limits
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import requests

from twitter.utils.helpers import truncate_tweet

if TYPE_CHECKING:
    from twitter.client import TwitterClient

log = logging.getLogger(__name__)

_NVD_BASE = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_NVD_PAGE_SIZE = 2000   # max items per request

# CVSS score → severity label
_SEVERITY_MAP = {
    "CRITICAL": (9.0, 10.0, "🔴"),
    "HIGH":     (7.0,  8.9, "🟠"),
    "MEDIUM":   (4.0,  6.9, "🟡"),
    "LOW":      (0.1,  3.9, "🟢"),
    "NONE":     (0.0,  0.0, "⚪"),
}

_DEFAULT_TWEET_TMPL = (
    "{severity_emoji} {severity} CVE: {cve_id}\n\n"
    "{description_short}\n\n"
    "CVSS: {cvss_score} | {cvss_vector_short}\n"
    "{nvd_url}\n"
    "#cybersecurity #CVE #infosec"
)


def _severity_from_score(score: float) -> str:
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return "NONE"


def _severity_emoji(severity: str) -> str:
    return _SEVERITY_MAP.get(severity.upper(), ("", "", "❓"))[2]


class CVEAlerts:
    """
    NIST NVD CVE tracker with Twitter alert integration.

    Parameters
    ----------
    client : TwitterClient
    nvd_api_key : str, optional
        NVD API key for higher rate limits.
    min_cvss_score : float
        Only tweet CVEs with CVSS score >= this value.
    tweet_template : str, optional
        Template string. Variables: {cve_id}, {severity}, {severity_emoji},
        {cvss_score}, {cvss_vector_short}, {description_short}, {nvd_url}.
    severities : list of str, optional
        Whitelist of severities to tweet: ['CRITICAL', 'HIGH', ...].
    keywords : list of str, optional
        Only tweet CVEs whose description contains one of these keywords.
    dry_run : bool
    request_delay : float
        Seconds between NVD API requests (no-key: ≥6s recommended).
    """

    def __init__(
        self,
        client: "TwitterClient",
        nvd_api_key: Optional[str] = None,
        min_cvss_score: float = 7.0,
        tweet_template: Optional[str] = None,
        severities: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        dry_run: bool = False,
        request_delay: float = 6.5,
    ):
        self.client = client
        self.nvd_api_key = nvd_api_key
        self.min_cvss_score = min_cvss_score
        self.tweet_template = tweet_template or _DEFAULT_TWEET_TMPL
        self.severities = [s.upper() for s in severities] if severities else ["CRITICAL", "HIGH"]
        self.keywords = [k.lower() for k in keywords] if keywords else []
        self.dry_run = dry_run
        self.request_delay = request_delay if nvd_api_key else max(request_delay, 6.0)
        self._db = client.db

        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        if nvd_api_key:
            self._session.headers["apiKey"] = nvd_api_key

    # ------------------------------------------------------------------
    # NVD API fetch
    # ------------------------------------------------------------------

    def _fetch_nvd_page(self, params: Dict) -> Dict:
        """Single NVD API request with retry."""
        for attempt in range(3):
            try:
                resp = self._session.get(_NVD_BASE, params=params, timeout=30)
                if resp.status_code == 403:
                    log.warning("NVD 403 — rate limited. Sleeping 35s…")
                    time.sleep(35)
                    continue
                if resp.status_code == 404:
                    return {}
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                log.warning("NVD request timeout (attempt %d/3)", attempt + 1)
                time.sleep(10)
            except Exception as exc:
                log.error("NVD request failed (attempt %d/3): %s", attempt + 1, exc)
                time.sleep(5)
        return {}

    def fetch_recent_cves(
        self,
        hours: int = 24,
        *,
        modified: bool = False,
        max_results: int = 500,
    ) -> List[Dict]:
        """
        Fetch CVEs published (or modified) in the last `hours` hours.

        Parameters
        ----------
        hours : int
            Look-back window in hours.
        modified : bool
            If True, use lastModStartDate/lastModEndDate instead of pubStartDate.
        max_results : int
            Maximum CVEs to return.

        Returns
        -------
        List of normalised CVE dicts (also cached in SQLite).
        """
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)

        date_fmt = "%Y-%m-%dT%H:%M:%S.000"
        if modified:
            date_params = {
                "lastModStartDate": since.strftime(date_fmt),
                "lastModEndDate": now.strftime(date_fmt),
            }
        else:
            date_params = {
                "pubStartDate": since.strftime(date_fmt),
                "pubEndDate": now.strftime(date_fmt),
            }

        all_cves: List[Dict] = []
        start_index = 0

        while len(all_cves) < max_results:
            params = {
                **date_params,
                "resultsPerPage": min(_NVD_PAGE_SIZE, max_results - len(all_cves)),
                "startIndex": start_index,
            }

            data = self._fetch_nvd_page(params)
            if not data:
                break

            total = data.get("totalResults", 0)
            vulnerabilities = data.get("vulnerabilities", [])

            for v in vulnerabilities:
                cve = self._normalise_cve(v.get("cve", {}))
                if cve:
                    all_cves.append(cve)
                    self._db.upsert_cve(self._cve_for_db(cve))

            start_index += len(vulnerabilities)
            if start_index >= total:
                break

            time.sleep(self.request_delay)

        log.info(
            "NVD: fetched %d CVEs (last %dh, modified=%s)", len(all_cves), hours, modified
        )
        return all_cves

    def search_cves(
        self,
        keyword: Optional[str] = None,
        cpe_name: Optional[str] = None,
        cwe_id: Optional[str] = None,
        cvss_severity: Optional[str] = None,
        max_results: int = 100,
    ) -> List[Dict]:
        """
        Search NVD for CVEs by keyword, CPE, CWE, or CVSS severity.

        Parameters
        ----------
        keyword : str, optional
            Search in CVE descriptions.
        cpe_name : str, optional
            CPE 2.3 name, e.g. 'cpe:2.3:a:apache:log4j:*'.
        cwe_id : str, optional
            CWE ID, e.g. 'CWE-79'.
        cvss_severity : str, optional
            'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'
        """
        params: Dict[str, Any] = {
            "resultsPerPage": min(_NVD_PAGE_SIZE, max_results),
            "startIndex": 0,
        }
        if keyword:
            params["keywordSearch"] = keyword
        if cpe_name:
            params["cpeName"] = cpe_name
        if cwe_id:
            params["cweId"] = cwe_id
        if cvss_severity:
            params["cvssV3Severity"] = cvss_severity.upper()

        all_cves: List[Dict] = []
        start = 0
        while len(all_cves) < max_results:
            params["startIndex"] = start
            data = self._fetch_nvd_page(params)
            if not data:
                break
            total = data.get("totalResults", 0)
            vulns = data.get("vulnerabilities", [])
            for v in vulns:
                cve = self._normalise_cve(v.get("cve", {}))
                if cve:
                    all_cves.append(cve)
                    self._db.upsert_cve(self._cve_for_db(cve))
            start += len(vulns)
            if start >= total or not vulns:
                break
            time.sleep(self.request_delay)

        log.info("NVD search: returned %d CVEs", len(all_cves))
        return all_cves

    def fetch_cve_by_id(self, cve_id: str) -> Optional[Dict]:
        """Fetch a specific CVE by its CVE-ID."""
        data = self._fetch_nvd_page({"cveId": cve_id})
        vulns = data.get("vulnerabilities", [])
        if not vulns:
            return None
        cve = self._normalise_cve(vulns[0].get("cve", {}))
        if cve:
            self._db.upsert_cve(self._cve_for_db(cve))
        return cve

    # ------------------------------------------------------------------
    # Tweet alerts
    # ------------------------------------------------------------------

    def tweet_new_cves(
        self,
        hours: int = 24,
        max_tweets: int = 5,
        *,
        min_cvss: Optional[float] = None,
        fetch_fresh: bool = True,
    ) -> List[Dict]:
        """
        Fetch recent CVEs and tweet any that pass filters and haven't been tweeted.

        Parameters
        ----------
        hours : int
            Look-back window for fetching.
        max_tweets : int
            Max tweets to post per call.
        min_cvss : float, optional
            Override self.min_cvss_score.
        fetch_fresh : bool
            If True, fetch from NVD before querying the local cache.
        """
        mc = min_cvss if min_cvss is not None else self.min_cvss_score

        if fetch_fresh:
            self.fetch_recent_cves(hours=hours)

        rows = self._db.get_untweeted_cves(min_score=mc, limit=max_tweets * 3)
        results: List[Dict] = []

        for row in rows:
            cve = dict(row)
            cve_id = cve["cve_id"]
            severity = cve.get("severity", "").upper()
            score = cve.get("cvss_score", 0.0) or 0.0

            # Severity filter
            if self.severities and severity not in self.severities:
                continue

            # Keyword filter
            if self.keywords:
                desc = (cve.get("description") or "").lower()
                if not any(kw in desc for kw in self.keywords):
                    continue

            desc_full = cve.get("description", "")
            desc_short = (desc_full[:200] + "…") if len(desc_full) > 200 else desc_full

            vector = cve.get("cvss_vector", "") or ""
            vector_short = vector.split("/")[0] if "/" in vector else vector

            nvd_url = f"https://nvd.nist.gov/vuln/detail/{cve_id}"

            tweet_text = self.tweet_template.format(
                cve_id=cve_id,
                severity=severity,
                severity_emoji=_severity_emoji(severity),
                cvss_score=f"{score:.1f}",
                cvss_vector_short=vector_short[:30],
                description_short=desc_short,
                nvd_url=nvd_url,
            )
            tweet_text = truncate_tweet(tweet_text)

            result: Dict[str, Any] = {
                "cve_id": cve_id,
                "severity": severity,
                "cvss_score": score,
                "tweet_text": tweet_text,
                "tweet_id": None,
                "dry_run": self.dry_run,
                "success": False,
            }

            if self.dry_run:
                log.info("[DRY-RUN] Would tweet CVE %s (%.1f %s)", cve_id, score, severity)
                result["success"] = True
                self._db.mark_cve_tweeted(cve_id, "dry_run")
            else:
                try:
                    resp = self.client.post_tweet(tweet_text)
                    tid = resp.get("id", "")
                    result["tweet_id"] = tid
                    result["success"] = True
                    self._db.mark_cve_tweeted(cve_id, tid)
                    log.info("✓ Tweeted CVE %s (score=%.1f) → tweet %s", cve_id, score, tid)
                    time.sleep(5)
                except Exception as exc:
                    result["error"] = str(exc)
                    log.error("✗ Failed to tweet CVE %s: %s", cve_id, exc)

            results.append(result)
            if len(results) >= max_tweets:
                break

        return results

    def get_cached_cves(
        self,
        min_score: float = 0.0,
        severity: Optional[str] = None,
        tweeted: Optional[bool] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """Return CVEs from the local SQLite cache."""
        wheres = ["cvss_score >= ?"]
        params: list = [min_score]
        if severity:
            wheres.append("severity = ?")
            params.append(severity.upper())
        if tweeted is not None:
            wheres.append("tweeted = ?")
            params.append(1 if tweeted else 0)
        where = " AND ".join(wheres)
        rows = self._db.fetchall(
            f"SELECT * FROM cve_cache WHERE {where} ORDER BY cvss_score DESC LIMIT ?",
            tuple(params) + (limit,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def _normalise_cve(self, cve_raw: Dict) -> Optional[Dict]:
        cve_id = cve_raw.get("id", "")
        if not cve_id:
            return None

        # Description (prefer English)
        descriptions = cve_raw.get("descriptions", [])
        desc = ""
        for d in descriptions:
            if d.get("lang") == "en":
                desc = d.get("value", "")
                break
        if not desc and descriptions:
            desc = descriptions[0].get("value", "")

        # CVSS v3 metrics (try v3.1 first, then v3.0)
        metrics = cve_raw.get("metrics", {})
        cvss_data: Optional[Dict] = None
        for key in ("cvssMetricV31", "cvssMetricV30"):
            entries = metrics.get(key, [])
            if entries:
                # prefer 'Primary' source
                primary = next(
                    (e for e in entries if e.get("type") == "Primary"), entries[0]
                )
                cvss_data = primary.get("cvssData", {})
                break

        score = 0.0
        vector = ""
        severity = "NONE"
        if cvss_data:
            score = float(cvss_data.get("baseScore", 0.0))
            vector = cvss_data.get("vectorString", "")
            severity = cvss_data.get("baseSeverity", _severity_from_score(score)).upper()

        # If no v3, fall back to v2
        if score == 0.0:
            v2_entries = metrics.get("cvssMetricV2", [])
            if v2_entries:
                v2 = v2_entries[0].get("cvssData", {})
                score = float(v2.get("baseScore", 0.0))
                vector = v2.get("vectorString", "")
                severity = _severity_from_score(score)

        # References
        refs = [
            r.get("url", "")
            for r in cve_raw.get("references", [])
            if r.get("url")
        ]

        # Affected CPEs
        affected: List[str] = []
        for config in cve_raw.get("configurations", []):
            for node in config.get("nodes", []):
                for cpe_match in node.get("cpeMatch", []):
                    if cpe_match.get("vulnerable"):
                        affected.append(cpe_match.get("criteria", ""))

        return {
            "id": cve_id,
            "description": desc,
            "severity": severity,
            "cvss_score": score,
            "cvss_vector": vector,
            "published_date": cve_raw.get("published", ""),
            "modified_date": cve_raw.get("lastModified", ""),
            "references": refs[:10],
            "affected": affected[:20],
        }

    def _cve_for_db(self, cve: Dict) -> Dict:
        return {
            "cve_id": cve["id"],
            "description": cve.get("description", ""),
            "severity": cve.get("severity", ""),
            "cvss_score": cve.get("cvss_score", 0.0),
            "cvss_vector": cve.get("cvss_vector", ""),
            "published_date": cve.get("published_date", ""),
            "modified_date": cve.get("modified_date", ""),
            "references_json": json.dumps(cve.get("references", [])),
            "affected_json": json.dumps(cve.get("affected", [])),
        }

    def close(self) -> None:
        self._session.close()
