#!/usr/bin/env python3
"""Re-time already-rendered time-lapse MP4s without re-rendering frames.
usage: python slow_videos.py FACTOR file.mp4 [file.mp4 ...]   (FACTOR 2 = half speed)"""
import subprocess, sys, os
import imageio_ffmpeg

ff = imageio_ffmpeg.get_ffmpeg_exe()
factor = float(sys.argv[1])
for p in sys.argv[2:]:
    tmp = p + ".tmp.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", p, "-vf", f"setpts={factor}*PTS", "-r", "15",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "slow", "-an", tmp], check=True)
    os.replace(tmp, p)
    print("slowed", p)
