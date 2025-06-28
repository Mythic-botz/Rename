import os
import re
import time
import shutil
import asyncio
import logging
from datetime import datetime
from PIL import Image
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaDocument, Message
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from plugins.antinsfw import check_anti_nsfw
from helper.utils import progress_for_pyrogram, humanbytes, convert
from helper.database import codeflixbots
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

renaming_operations = {}

def normalize_filename_for_extraction(filename):
    if not filename:
        return filename
    if len(filename) > 64 and filename.count("_") > 3 and filename.count(" ") < 2:
        name, ext = os.path.splitext(filename)
        return name.replace("_", " ") + ext
    return filename

SEASON_EPISODE_PATTERNS = [
    (re.compile(r'S(\d{1,2})[_\- ]E(\d{1,3})', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'S(\d{1,2})[_\- ](\d{1,3})', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'[_\- ]E(\d{1,3})', re.IGNORECASE), (None, 'episode')),
    (re.compile(r'[_\- ](\d{1,3})[_\- ]', re.IGNORECASE), (None, 'episode')),
    (re.compile(r'\[S(\d{1,2})[\s\-]+E(\d{1,3})\]', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'\[S(\d{1,2})[\s\-]+(\d{1,3})\]', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'\[S(\d{1,2})\s+E(\d{1,3})\]', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'\[S\s*(\d{1,2})\s*E\s*(\d{1,3})\]', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'S(\d{1,2})[\s\-]+E(\d{1,3})', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'S(\d{1,2})[\s\-]+(\d{1,3})', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'S(\d+)(?:E|EP)(\d+)'), ('season', 'episode')),
    (re.compile(r'S(\d+)[\s-]*(?:E|EP)(\d+)'), ('season', 'episode')),
    (re.compile(r'Season\s*(\d+)\s*Episode\s*(\d+)', re.IGNORECASE), ('season', 'episode')),
    (re.compile(r'\[S(\d+)\]\[E(\d+)\]'), ('season', 'episode')),
    (re.compile(r'S(\d+)[^\d]+(\d{1,3})\b'), ('season', 'episode')),
    (re.compile(r'(?:E|EP|Episode)\s*(\d+)', re.IGNORECASE), (None, 'episode')),
    (re.compile(r'\b(\d{1,3})\b'), (None, 'episode'))
]


QUALITY_PATTERNS = [
    (re.compile(r'(?<!\d)(144p)(?!\d)', re.IGNORECASE), lambda m: "144p"),
    (re.compile(r'(?<!\d)(240p)(?!\d)', re.IGNORECASE), lambda m: "240p"),
    (re.compile(r'(?<!\d)(360p)(?!\d)', re.IGNORECASE), lambda m: "360p"),
    (re.compile(r'(?<!\d)(480p)(?!\d)', re.IGNORECASE), lambda m: "480p"),
    (re.compile(r'(?<!\d)(720p)(?!\d)', re.IGNORECASE), lambda m: "720p"),
    (re.compile(r'(?<!\d)(1080p)(?!\d)', re.IGNORECASE), lambda m: "1080p"),
    (re.compile(r'(?<!\d)(1440p)(?!\d)', re.IGNORECASE), lambda m: "1440p"),
    (re.compile(r'(?<!\d)(2160p)(?!\d)', re.IGNORECASE), lambda m: "2160p"),
    (re.compile(r'\b4k\b', re.IGNORECASE), lambda m: "2160p"),
    (re.compile(r'[_\-. ](144p|240p|360p|480p|720p|1080p|1440p|2160p)(?=[_\-. ])', re.IGNORECASE), lambda m: m.group(1)),
    (re.compile(r'\bHDRip\b', re.IGNORECASE), lambda m: "HDRip"),
]

def extract_season_episode(filename):
    filename = re.sub(r'\(.*?\)', ' ', filename)
    for pattern, (season_group, episode_group) in SEASON_EPISODE_PATTERNS:
        match = pattern.search(filename)
        if match:
            season = episode = None
            if season_group:
                season = match.group(1).zfill(2) if match.group(1) else "01"
            if episode_group:
                episode = match.group(2 if season_group else 1).zfill(2)
            logger.info(f"Extracted season: {season}, episode: {episode} from {filename}")
            return season or "01", episode
    logger.warning(f"No season/episode pattern matched for {filename}")
    return "01", None

def extract_quality(filename):
    seen = set()
    quality_parts = []
    for pattern, extractor in QUALITY_PATTERNS:
        match = pattern.search(filename)
        if match:
            quality = extractor(match).lower()
            if quality not in seen:
                quality_parts.append(quality)
                seen.add(quality)
                filename = filename.replace(match.group(0), '', 1)
    resolution_qualities = [q for q in quality_parts if q.endswith("p") or q == "4k" or q == "2160p"]
    if resolution_qualities:
        return resolution_qualities[0] 
    elif "hdrip" in quality_parts:
        return "HDRip"
    
    return "Unknown"

async def detect_audio_info(file_path):
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError("ffprobe not found in PATH")

    cmd = [
        ffprobe,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        file_path
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    try:
        info = json.loads(stdout)
        streams = info.get('streams', [])
        
        audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
        sub_streams = [s for s in streams if s.get('codec_type') == 'subtitle']

        japanese_audio = 0
        english_audio = 0
        for audio in audio_streams:
            lang = audio.get('tags', {}).get('language', '').lower()
            if lang in {'ja', 'jpn', 'japanese'}:
                japanese_audio += 1
            elif lang in {'en', 'eng', 'english'}:
                english_audio += 1

        english_subs = len([
            s for s in sub_streams 
            if s.get('tags', {}).get('language', '').lower() in {'en', 'eng', 'english'}
        ])

        return len(audio_streams), len(sub_streams), japanese_audio, english_audio, english_subs
    except Exception as e:
        logger.error(f"Audio detection error: {e}")
        return 0, 0, 0, 0, 0

def get_audio_label(audio_info):
    audio_count, sub_count, jp_audio, en_audio, en_subs = audio_info
    
    if audio_count == 1:
        if jp_audio >= 1 and en_subs >= 1:
            return "Sub" + ("s" if sub_count > 1 else "")
        if en_audio >= 1:
            return "Dub"
    
    if audio_count == 2:
        return "Dual"
    elif audio_count == 3:
        return "Tri"
    elif audio_count >= 4:
        return "Multi"
    
    return "Unknown"

async def detect_video_resolution(file_path):
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError("ffprobe not found in PATH")

    cmd = [
        ffprobe,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        '-select_streams', 'v',
        file_path
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    try:
        info = json.loads(stdout)
        streams = info.get('streams', [])
        
        if not streams:
            return "Unknown"
            
        video_stream = streams[0]
        width = video_stream.get('width', 0)
        height = video_stream.get('height', 0)
        
        if height >= 2160 or width >= 3840:
            return "2160p"
        elif height >= 1440:
            return "1440p"
        elif height >= 1080:
            return "1080p"
        elif height >= 720:
            return "720p"
        elif height >= 480:
            return "480p"
        elif height >= 360:
            return "360p"
        elif height >= 240:
            return "240p"
        elif height >= 144:
            return "144p"
        else:
            return f"{height}p"
            
    except Exception as e:
        logger.error(f"Resolution detection error: {e}")
        return "Unknown"

async def cleanup_files(*paths):
    """Safely remove files if they exist"""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.error(f"Error removing {path}: {e}")

async def process_thumbnail(thumb_path):
    """Process and resize thumbnail image"""
    if not thumb_path or not os.path.exists(thumb_path):
        return None
    
    try:
        with Image.open(thumb_path) as img:
            img = img.convert("RGB").resize((320, 320))
            img.save(thumb_path, "JPEG")
        return thumb_path
    except Exception as e:
        logger.error(f"Thumbnail processing failed: {e}")
        await cleanup_files(thumb_path)
        return None

async def add_metadata(input_path, output_path, user_id):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found in PATH")

    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)

    metadata = {
        'title': await codeflixbots.get_title(user_id),
        'video': await codeflixbots.get_video(user_id),
        'audio': await codeflixbots.get_audio(user_id),
        'subtitle': await codeflixbots.get_subtitle(user_id),
        'artist': await codeflixbots.get_artist(user_id),
        'author': await codeflixbots.get_author(user_id),
        'encoded_by': await codeflixbots.get_encoded_by(user_id),
        'custom_tag': await codeflixbots.get_custom_tag(user_id)
    }

    cmd = [
        ffmpeg,
        '-hide_banner',
        '-i', input_path,
        '-map', '0',
        '-c', 'copy',
        '-metadata', f'title={metadata["title"]}',
        '-metadata:s:v', f'title={metadata["video"]}',
        '-metadata:s:s', f'title={metadata["subtitle"]}',
        '-metadata:s:a', f'title={metadata["audio"]}',
        '-metadata', f'artist={metadata["artist"]}',
        '-metadata', f'author={metadata["author"]}',
        '-metadata', f'encoded_by={metadata["encoded_by"]}',
        '-metadata', f'comment={metadata["custom_tag"]}',
        '-loglevel', 'error',
        '-y'
    ]

    cmd += ['-f', 'matroska', output_path]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
    
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(f"FFmpeg error: {error_msg}")
            if os.path.exists(output_path):
                await aiofiles.os.remove(output_path)
            
            raise RuntimeError(f"Metadata addition failed: {error_msg}")
    
        return output_path

    except Exception as e:
        logger.error(f"Metadata processing failed: {e}")
        await cleanup_files(output_path)
        raise

@Client.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def auto_rename_files(client, message):
    """Main handler for auto-renaming files"""
    user_id = message.from_user.id
    format_template = await codeflixbots.get_format_template(user_id)
    
    if not format_template:
        return await message.reply_text("Please set a rename format using /autorename")

    # Get file information
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
        file_size = message.document.file_size
        media_type = "document"
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "video"
        file_size = message.video.file_size
        media_type = "video"
    elif message.audio:
        file_id = message.audio.file_id
        file_name = message.audio.file_name or "audio"
        file_size = message.audio.file_size
        media_type = "audio"
    else:
        return await message.reply_text("Unsupported file type")

    # Prevent duplicate processing
    if file_id in renaming_operations:
        if (datetime.now() - renaming_operations[file_id]).seconds < 10:
            return
    renaming_operations[file_id] = datetime.now()

    try:
        # Extract metadata from filename
        season, episode = extract_season_episode(file_name)
        quality = extract_quality(file_name)
        
        # Replace placeholders in template
        replacements = {
            '{season}': season or 'XX',
            '{episode}': episode or 'XX',
            '{quality}': quality,
            'Season': season or 'XX',
            'Episode': episode or 'XX',
            'QUALITY': quality
        }
        
        for placeholder, value in replacements.items():
            format_template = format_template.replace(placeholder, value)

        # Prepare file paths
        ext = os.path.splitext(file_name)[1] or ('.mp4' if media_type == 'video' else '.mp3')
        new_filename = f"{format_template}{ext}"
        download_path = f"downloads/{new_filename}"
        metadata_path = f"metadata/{new_filename}"
        
        os.makedirs(os.path.dirname(download_path), exist_ok=True)
        os.makedirs(os.path.dirname(metadata_path), exist_ok=True)

        # Download file
        msg = await message.reply_text("**Downloading...**")
        try:
            file_path = await client.download_media(
                message,
                file_name=download_path,
                progress=progress_for_pyrogram,
                progress_args=("Downloading...", msg, time.time())
            )
        except Exception as e:
            await msg.edit(f"Download failed: {e}")
            raise

        # Process metadata
        await msg.edit("**Processing metadata...**")
        try:
            await add_metadata(file_path, metadata_path, user_id)
            file_path = metadata_path
        except Exception as e:
            await msg.edit(f"Metadata processing failed: {e}")
            raise

        # Prepare for upload
        await msg.edit("**Preparing upload...**")
        caption = await codeflixbots.get_caption(message.chat.id) or f"**{new_filename}**"
        thumb = await codeflixbots.get_thumbnail(message.chat.id)
        thumb_path = None

        # Handle thumbnail
        if thumb:
            thumb_path = await client.download_media(thumb)
        elif media_type == "video" and message.video.thumbs:
            thumb_path = await client.download_media(message.video.thumbs[0].file_id)
        
        thumb_path = await process_thumbnail(thumb_path)

        # Upload file
        await msg.edit("**Uploading...**")
        try:
            upload_params = {
                'chat_id': message.chat.id,
                'caption': caption,
                'thumb': thumb_path,
                'progress': progress_for_pyrogram,
                'progress_args': ("Uploading...", msg, time.time())
            }

            if media_type == "document":
                await client.send_document(document=file_path, **upload_params)
            elif media_type == "video":
                await client.send_video(video=file_path, **upload_params)
            elif media_type == "audio":
                await client.send_audio(audio=file_path, **upload_params)

            await msg.delete()
        except Exception as e:
            await msg.edit(f"Upload failed: {e}")
            raise

    except Exception as e:
        logger.error(f"Processing error: {e}")
        await message.reply_text(f"Error: {str(e)}")
    finally:
        # Clean up files
        await cleanup_files(download_path, metadata_path, thumb_path)
        renaming_operations.pop(file_id, None)
