"""Small round-related queries shared across routes."""
from sqlalchemy.orm import Session

from .models import Round


def get_open_round(session: Session) -> Round | None:
    """The round participants currently submit into. At most one at a time
    in this design -- Phase 1 doesn't support overlapping rounds."""
    return session.query(Round).filter(Round.state == "OPEN").order_by(Round.id.desc()).first()
