import yt_dlp
opts = {"remote_components": ["ejs:github"], "quiet": True}
try:
    with yt_dlp.YoutubeDL(opts) as ydl:
        print(f"remote_components: {ydl.params.get('remote_components')}")
except Exception as e:
    print(f"Error: {e}")
