from gi.repository import GObject

from .models import AppEntry


class AppItem(GObject.Object):
    __gtype_name__ = "AppItem"

    name = GObject.Property(type=str, default="")
    manager = GObject.Property(type=str, default="")
    source = GObject.Property(type=str, default="")
    version = GObject.Property(type=str, default="")
    installed_at = GObject.Property(type=str, default="")
    category = GObject.Property(type=str, default="")
    details = GObject.Property(type=str, default="")
    package_id = GObject.Property(type=str, default="")

    marked = GObject.Property(type=bool, default=False)
    marked_label = GObject.Property(type=str, default="")

    icon = GObject.Property(type=str, default="")
    is_gui_app = GObject.Property(type=bool, default=True)
    exec_name = GObject.Property(type=str, default="")
    is_duplicate = GObject.Property(type=bool, default=False)

    def __init__(self, app: AppEntry):
        super().__init__()

        self.name = app.name
        self.manager = app.manager
        self.source = app.source
        self.package_id = app.package_id
        self.version = app.version or ""
        self.installed_at = app.installed_at or ""
        self.category = app.category or "Other"
        self.details = app.details or ""

        self.marked = False
        self.marked_label = ""

        self.icon = app.icon or ""
        self.is_gui_app = getattr(app, "is_gui_app", True)
        self.exec_name = getattr(app, "exec_name", "") or ""
        self.is_duplicate = getattr(app, "is_duplicate", False)
