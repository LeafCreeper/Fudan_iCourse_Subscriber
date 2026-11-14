"""Exercise the normal video source and stream path for one lecture."""

import os
import subprocess

from main import login_with_retry
from src.api.icourse import ICourseClient


def main():
    course_id = os.environ["COURSE_ID"]
    sub_id = os.environ["SUB_ID"]
    if not course_id or not sub_id:
        raise ValueError("COURSE_ID and SUB_ID are required")

    vpn = login_with_retry()
    try:
        client = ICourseClient(vpn)
        url = client.get_video_url(course_id, sub_id)
        if not url:
            raise RuntimeError("No video source resolved")
        print("Video source resolved", flush=True)

        vpn_url, headers = client.get_stream_params(url)
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-headers", headers,
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5", "-i", vpn_url,
                "-map", "0:a:0", "-t", "2", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Video stream failed (ffmpeg exit {result.returncode})")
        print("Video stream opened", flush=True)
    finally:
        vpn.session.close()


if __name__ == "__main__":
    main()
