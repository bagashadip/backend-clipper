import yt_dlp
opts = {"source_address": "::", "quiet": True}
with yt_dlp.YoutubeDL(opts) as ydl:
    print("yt-dlp initialized with source_address successfully")
