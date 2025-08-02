import re
import asyncio
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram import Client

active_sequences = {}
message_ids = {}

import logging
logger = logging.getLogger(__name__)

@Client.on_message(filters.command("ssequence") & filters.private)
@check_ban_status
async def start_sequence(client, message: Message):
    user_id = message.from_user.id
        
    if user_id in active_sequences:
        await message.reply_text("**A sᴇǫᴜᴇɴᴄᴇ ɪs ᴀʟʀᴇᴀᴅʏ ᴀᴄᴛɪᴠᴇ! Usᴇ /esequence ᴛᴏ ᴇɴᴅ ɪᴛ.**")
    else:
        active_sequences[user_id] = []
        message_ids[user_id] = []
        msg = await message.reply_text("**Sᴇǫᴜᴇɴᴄᴇ ʜᴀs ʙᴇᴇɴ sᴛᴀʀᴛᴇᴅ! Sᴇɴᴅ ʏᴏᴜʀ ғɪʟᴇs...**")
        message_ids[user_id].append(msg.id)

@Client.on_message(filters.command("esequence") & filters.private)
@check_ban_status
async def end_sequence(client, message: Message):
    user_id = message.from_user.id
    
    if user_id not in active_sequences:
        return await message.reply_text("**Nᴏ ᴀᴄᴛɪᴠᴇ sᴇǫᴜᴇɴᴄᴇ ғᴏᴜɴᴅ!**\n**Usᴇ /ssequence ᴛᴏ sᴛᴀʀᴛ ᴏɴᴇ.**")

    file_list = active_sequences.pop(user_id, [])
    delete_messages = message_ids.pop(user_id, [])

    if not file_list:
        return await message.reply_text("**Nᴏ ғɪʟᴇs ʀᴇᴄᴇɪᴠᴇᴅ ɪɴ ᴛʜɪs sᴇǫᴜᴇɴᴄᴇ!**")

    quality_order = {
        "144p": 1, "240p": 2, "360p": 3, "480p": 4,
        "720p": 5, "1080p": 6, "1440p": 7, "2160p": 8
    }

    def extract_quality(filename):
        filename = filename.lower()
        patterns = [
            (r'2160p|4k', '2160p'),
            (r'1440p|2k', '1440p'),
            (r'1080p|fhd', '1080p'),
            (r'720p|hd', '720p'),
            (r'480p|sd', '480p'),
            (r'(\d{3,4})p', lambda m: f"{m.group(1)}p")
        ]
        
        for pattern, value in patterns:
            match = re.search(pattern, filename)
            if match:
                return value if isinstance(value, str) else value(match)
        return "unknown"

    def sorting_key(f):
        filename = f["file_name"].lower()
        
        season = episode = 0
        season_match = re.search(r's(\d+)', filename)
        episode_match = re.search(r'e(\d+)', filename) or re.search(r'ep?(\d+)', filename)
        
        if season_match:
            season = int(season_match.group(1))
        if episode_match:
            episode = int(episode_match.group(1))
            
        quality = extract_quality(filename)
        quality_priority = quality_order.get(quality.lower(), 9)
        
        padded_episode = f"{episode:04d}"
        
        return (season, padded_episode, quality_priority, filename)

    try:
        sorted_files = sorted(file_list, key=sorting_key)
        await message.reply_text(f"**Sᴇǫᴜᴇɴᴄᴇ ᴄᴏᴍᴘʟᴇᴛᴇᴅ!\nSᴇɴᴅɪɴɢ {len(sorted_files)} ғɪʟᴇs ɪɴ ᴏʀᴅᴇʀ...**")

        for index, file in enumerate(sorted_files):
            try:
                sent_msg = await client.send_document(
                    message.chat.id,
                    file["file_id"],
                    caption=f"**{file['file_name']}**",
                    parse_mode=ParseMode.MARKDOWN
                )

                if index < len(sorted_files) - 1:
                    await asyncio.sleep(0.5)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                logger.error(f"Error sending file {file['file_name']}: {e}")

        if delete_messages:
            await client.delete_messages(message.chat.id, delete_messages)

    except Exception as e:
        logger.error(f"Sequence processing failed: {e}")
        await message.reply_text("**Fᴀɪʟᴇᴅ ᴛᴏ ᴘʀᴏᴄᴇss sᴇǫᴜᴇɴᴄᴇ! Cʜᴇᴄᴋ ʟᴏɢs ғᴏʀ ᴅᴇᴛᴀɪʟs.**")
