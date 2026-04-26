from twitter.config import Config

cfg = Config.from_env(".env")
c = cfg.credentials

print("=== Credentials loaded ===")
print(f"API_KEY:              {c.api_key[:6]}...{c.api_key[-4:]}" if c.api_key else "API_KEY:              (empty!)")
print(f"API_SECRET:           {c.api_secret[:6]}...{c.api_secret[-4:]}" if c.api_secret else "API_SECRET:           (empty!)")
print(f"ACCESS_TOKEN:         {c.access_token[:6]}...{c.access_token[-4:]}" if c.access_token else "ACCESS_TOKEN:         (empty!)")
print(f"ACCESS_TOKEN_SECRET:  {c.access_token_secret[:6]}...{c.access_token_secret[-4:]}" if c.access_token_secret else "ACCESS_TOKEN_SECRET:  (empty!)")
print(f"BEARER_TOKEN:         {c.bearer_token[:6]}..." if c.bearer_token else "BEARER_TOKEN:         (empty!)")
