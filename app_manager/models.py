from dataclasses import dataclass
from typing import Optional


@dataclass
class AppEntry:
    name: str
    manager: str
    source: str
    package_id: str
    version: str = ""
    installed_at: Optional[str] = None
    category: str = "Other"
    is_gui_app: bool = True
    details: str = ""
    icon: str = ""
    exec_name: str = ""
