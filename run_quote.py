from twitter.config import Config
from twitter.bot import Bot

cfg = Config.from_env(".env")
cfg.quote_tweet.dry_run = False  # change to False to post for real

bot = Bot(cfg)
result = bot.quote_tweet.quote_specific_tweet("2047881966268117064", "WOW")
print(result)
bot.close()
