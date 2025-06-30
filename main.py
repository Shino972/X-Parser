import asyncio
import tweepy
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto
from aiogram.filters import Command
import aiosqlite
from typing import Tuple, Optional

from config import (
    TWITTER_API_KEY,
    TWITTER_API_SECRET,
    TWITTER_ACCESS_TOKEN,
    TWITTER_ACCESS_TOKEN_SECRET,
    TWITTER_BEARER_TOKEN,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHANNEL_ID,
    authors
)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

twitter_client = tweepy.Client(
    bearer_token=TWITTER_BEARER_TOKEN,
    consumer_key=TWITTER_API_KEY,
    consumer_secret=TWITTER_API_SECRET,
    access_token=TWITTER_ACCESS_TOKEN,
    access_token_secret=TWITTER_ACCESS_TOKEN_SECRET
)

async def init_db():
    async with aiosqlite.connect("database.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS last_checked (
                author_username TEXT PRIMARY KEY,
                last_tweet_id TEXT,
                last_check_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tweets (
                tweet_id TEXT PRIMARY KEY,
                author TEXT,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                message_id INTEGER
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_votes (
                user_id INTEGER,
                tweet_id TEXT,
                vote_type TEXT,
                PRIMARY KEY (user_id, tweet_id)
            )
        """)
        await db.commit()

async def get_last_tweet_id(author_username: str) -> Optional[str]:
    async with aiosqlite.connect("database.db") as db:
        cursor = await db.execute(
            "SELECT last_tweet_id FROM last_checked WHERE author_username = ?",
            (author_username,)
        )
        result = await cursor.fetchone()
        return result[0] if result else None

async def update_last_tweet(author_username: str, tweet_id: str):
    async with aiosqlite.connect("database.db") as db:
        await db.execute(
            """INSERT OR REPLACE INTO last_checked (author_username, last_tweet_id)
               VALUES (?, ?)""",
            (author_username, tweet_id)
        )
        await db.commit()

async def get_tweet_stats(tweet_id: str) -> Tuple[int, int]:
    async with aiosqlite.connect("database.db") as db:
        cursor = await db.execute(
            "SELECT likes, dislikes FROM tweets WHERE tweet_id = ?",
            (tweet_id,)
        )
        result = await cursor.fetchone()
        return result if result else (0, 0)

async def update_tweet_stats(tweet_id: str, author: str, message_id: int):
    async with aiosqlite.connect("database.db") as db:
        await db.execute(
            """INSERT OR IGNORE INTO tweets (tweet_id, author, likes, dislikes, message_id)
               VALUES (?, ?, 0, 0, ?)""",
            (tweet_id, author, message_id)
        )
        await db.commit()

async def process_vote(callback: CallbackQuery, tweet_id: str, vote_type: str) -> Optional[Tuple[int, int]]:
    user_id = callback.from_user.id
    
    async with aiosqlite.connect("database.db") as db:
        cursor = await db.execute(
            "SELECT vote_type FROM user_votes WHERE user_id = ? AND tweet_id = ?",
            (user_id, tweet_id)
        )
        existing_vote = await cursor.fetchone()
        
        if existing_vote:
            previous_vote = existing_vote[0]
            if previous_vote == vote_type:
                await db.execute(
                    "DELETE FROM user_votes WHERE user_id = ? AND tweet_id = ?",
                    (user_id, tweet_id)
                )
                
                if vote_type == "like":
                    await db.execute(
                        "UPDATE tweets SET likes = likes - 1 WHERE tweet_id = ?",
                        (tweet_id,)
                    )
                else:
                    await db.execute(
                        "UPDATE tweets SET dislikes = dislikes - 1 WHERE tweet_id = ?",
                        (tweet_id,)
                    )
            else:
                await db.execute(
                    "UPDATE user_votes SET vote_type = ? WHERE user_id = ? AND tweet_id = ?",
                    (vote_type, user_id, tweet_id)
                )
                
                if vote_type == "like":
                    await db.execute(
                        "UPDATE tweets SET likes = likes + 1, dislikes = dislikes - 1 WHERE tweet_id = ?",
                        (tweet_id,)
                    )
                else:
                    await db.execute(
                        "UPDATE tweets SET likes = likes - 1, dislikes = dislikes + 1 WHERE tweet_id = ?",
                        (tweet_id,)
                    )
        else:
            await db.execute(
                "INSERT INTO user_votes (user_id, tweet_id, vote_type) VALUES (?, ?, ?)",
                (user_id, tweet_id, vote_type)
            )
            
            if vote_type == "like":
                await db.execute(
                    "UPDATE tweets SET likes = likes + 1 WHERE tweet_id = ?",
                    (tweet_id,)
                )
            else:
                await db.execute(
                    "UPDATE tweets SET dislikes = dislikes + 1 WHERE tweet_id = ?",
                    (tweet_id,)
                )
        
        await db.commit()
        
        cursor = await db.execute(
            "SELECT likes, dislikes FROM tweets WHERE tweet_id = ?",
            (tweet_id,)
        )
        return await cursor.fetchone()

def create_vote_keyboard(tweet_id: str, likes: int = 0, dislikes: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"👍 {likes}", callback_data=f"like_{tweet_id}"),
            InlineKeyboardButton(text=f"👎 {dislikes}", callback_data=f"dislike_{tweet_id}")
        ]
    ])

async def check_new_tweets():
    for author in authors:
        try:
            user = twitter_client.get_user(username=author)
            
            tweets = twitter_client.get_users_tweets(
                user.data.id,
                max_results=5,
                exclude_replies=True,
                tweet_fields=["created_at", "attachments", "public_metrics"]
            )
            
            if not tweets.data:
                continue
                
            newest_tweet = tweets.data[0]
            last_tweet_id = await get_last_tweet_id(author)
            
            if last_tweet_id is None:
                await update_last_tweet(author, newest_tweet.id)
                continue
                
            if newest_tweet.id != last_tweet_id:
                
                tweet_url = f"https://twitter.com/{author}/status/{newest_tweet.id}"
                message_text = f"<b>Author @{author}:\n\n{tweet_url}</b>"
                
                likes, dislikes = await get_tweet_stats(newest_tweet.id)
                keyboard = create_vote_keyboard(newest_tweet.id, likes, dislikes)
                
                if hasattr(newest_tweet, 'attachments'):
                    media_urls = await get_tweet_media(newest_tweet.id)
                    if media_urls:
                        await send_media_to_channel(media_urls, message_text, keyboard, newest_tweet.id, author)
                else:
                    message = await bot.send_message(
                        TELEGRAM_CHANNEL_ID,
                        message_text,
                        reply_markup=keyboard
                    )
                    await update_tweet_stats(newest_tweet.id, author, message.message_id)
                
                await update_last_tweet(author, newest_tweet.id)
                
        except Exception:
            pass

async def get_tweet_media(tweet_id: str) -> Optional[list]:
    try:
        tweet = twitter_client.get_tweet(
            tweet_id,
            expansions=["attachments.media_keys"],
            media_fields=["url", "preview_image_url", "type"]
        )
        
        if not tweet.includes or 'media' not in tweet.includes:
            return None
            
        media_urls = []
        for media in tweet.includes['media']:
            if media.type in ['photo', 'animated_gif']:
                url = media.url or media.preview_image_url
                if url:
                    media_urls.append(url)
        
        return media_urls if media_urls else None
        
    except Exception:
        return None

async def send_media_to_channel(media_urls: list, caption: str, keyboard: InlineKeyboardMarkup, tweet_id: str, author: str):
    try:
        first_photo = media_urls[0]
        message = await bot.send_photo(
            TELEGRAM_CHANNEL_ID,
            photo=first_photo,
            caption=caption,
            reply_markup=keyboard
        )
        
        if len(media_urls) > 1:
            media_group = [
                InputMediaPhoto(media=url)
                for url in media_urls[1:10]
            ]
            await bot.send_media_group(TELEGRAM_CHANNEL_ID, media=media_group)
        
        await update_tweet_stats(tweet_id, author, message.message_id)
        
    except Exception:
        pass

@dp.callback_query(F.data.startswith("like_") | F.data.startswith("dislike_"))
async def handle_vote(callback: CallbackQuery):
    try:
        action, tweet_id = callback.data.split('_')
        stats = await process_vote(callback, tweet_id, action)
        
        if stats:
            likes, dislikes = stats
            keyboard = create_vote_keyboard(tweet_id, likes, dislikes)
            
            try:
                await callback.message.edit_reply_markup(reply_markup=keyboard)
            except Exception:
                pass
            
            await callback.answer("✅")
        else:
            pass
            
    except Exception:
        pass

async def scheduled_checker(minutes=10):
    while True:
        await check_new_tweets()
        await asyncio.sleep(minutes * 60)

@dp.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer("{TELEGRAM_CHANNEL_ID}")

async def main():
    await init_db()
    asyncio.create_task(scheduled_checker(minutes=10))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())