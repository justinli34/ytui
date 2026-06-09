from pydantic import BaseModel


class VideoSearchResult(BaseModel):
    title: str
    url: str
    channel: str
    duration: float | None
