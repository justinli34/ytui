import yt_dlp

from ytui.models import VideoSearchResult


def search(query: str, num_results: int) -> list[VideoSearchResult]:
    videos = []

    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
        results = ydl.extract_info(f"ytsearch{num_results}:{query}", download=False)

        for result in results.get("entries", []):
            # Exclude channels, playlists, and other non-video results
            if result.get("ie_key") != "Youtube":
                continue

            videos.append(
                VideoSearchResult(
                    title=result.get("title"),
                    url=result.get("url"),
                    channel=result.get("uploader"),
                    duration=result.get("duration"),
                )
            )

    return videos
