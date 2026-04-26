import requests
from requests_oauthlib import OAuth1

# Paste your credentials here directly to test
API_KEY             = "1713421497329754112-FRHcIHKxgBhR3yTBOyYROdoitMFf4M"  # TWITTER_API_KEY
API_SECRET          = "gAhgmdvYAqZ3IRaTS2Jg4PqkbupCHwc5s5YWdiPn5OGRB"  # TWITTER_API_SECRET
ACCESS_TOKEN        = "1713421497329754112-VHp5JcQD1YItmhIdtwjvMVNVMn0LZP"  # TWITTER_ACCESS_TOKEN
ACCESS_TOKEN_SECRET = "YV1eWnKJJI9FEc1XhDQNH6BHiSjvxEho5zm2mWIe61GMA"  # TWITTER_ACCESS_TOKEN_SECRET

auth = OAuth1(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET)

# Try posting a tweet
resp = requests.post(
    "https://api.twitter.com/2/tweets",
    json={"text": "test tweet from script"},
    auth=auth,
)

print("Status:", resp.status_code)
print("Response:", resp.json())
