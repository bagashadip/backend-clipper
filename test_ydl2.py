import yt_dlp
opts = {"force_ipv6": True, "quiet": True}
with yt_dlp.YoutubeDL(opts) as ydl:
    print(f"force_ipv6: {ydl.params.get('force_ipv6')}")
