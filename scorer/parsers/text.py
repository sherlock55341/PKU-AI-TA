from models import Attachment


def parse(attachment: Attachment) -> str:
    """Decode plain-text attachment to string."""
    return attachment.data.decode("utf-8", errors="replace")
