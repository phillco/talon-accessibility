from dataclasses import dataclass, field
from itertools import chain
from typing import Optional
from uuid import UUID

from talon import Context, Module, actions, app, cron, imgui, settings, ui

# XXX(nriley) actions are being returned out of order; that's a problem if we want to pop up a menu

mod = Module()

mod.list("notification_actions", desc="Notification actions")
mod.list("notification_apps", desc="Notification apps")

mod.setting(
    "notification_debug",
    type=bool,
    default=False,
    desc="Display macOS notification debugging information.",
)

try:
    from rich.console import Console

    console = Console(color_system="truecolor", soft_wrap=True)

    def debug_print(obj: any, *args):
        """Pretty prints the object"""
        if not settings.get("user.notification_debug"):
            return
        if args:
            console.out(obj, *args)
        else:
            console.print(obj)

except ImportError:

    def debug_print(obj: any, *args):
        if not settings.get("user.notification_debug"):
            return
        print(obj, *args)


@mod.action_class
class Actions:
    def notification_action(index: int, action: str) -> bool:
        """Perform the specified action on the notification (stack) at the specified index"""
        return False

    def notification_app_action(app_name: str, action: str) -> bool:
        """Perform the specified action on the first notification (stack) for the specified app"""
        return False

    def notification_show_actions(index: int):
        """Display actions available on the notification at the specified index, or hide list if index is -1"""

    def notifications_update():
        """Update notification list to reflect what is currently onscreen"""
        # (poll? not try to keep up? not sure what else to do)

    def notification_center():
        """Display or hide Notification Center"""


@dataclass(frozen=True)
class Notification:
    identifier: int
    subrole: str = field(default=None, compare=False)
    app_name: str = field(default=None, compare=False)
    stacking_identifier: str = field(default=None, compare=False)
    title: str = field(default=None, compare=False)
    subtitle: str = field(default=None, compare=False)
    body: str = field(default=None, compare=False)
    # action values are named "Name:<name>\nTarget:0x0\nSelector:(null)"; keys are speakable
    actions: dict[str, str] = field(default=None, compare=False)

    @staticmethod
    def group_identifier(group):
        identifier = getattr(group, "AXIdentifier", None)

        if identifier is None:
            return None
        # For macOS pre-Sequoia.
        if str.isdigit(identifier):
            return int(identifier)
        # For macOS post-Sequoia.
        try:
            return UUID(identifier)
        except ValueError:
            return None

    @staticmethod
    def from_group(group, identifier):
        # XXX(nriley) better handle AXNotificationCenterBannerStack
        group_actions = group.actions
        if "AXScrollToVisible" in group_actions:
            del group_actions["AXScrollToVisible"]  # not useful
        # XXX(nriley) create_spoken_forms_from_list doesn't handle apostrophes correctly
        # https://github.com/knausj85/knausj_talon/issues/780
        group_actions = {
            name.lower().replace("’", "'"): action
            for action, name in group_actions.items()
        }

        title = body = subtitle = None

        try:
            title = group.children.find_one(AXIdentifier="title").AXValue
        except ui.UIErr:
            pass

        try:
            body = group.children.find_one(AXIdentifier="body").AXValue
        except ui.UIErr:
            pass

        try:
            subtitle = group.children.find_one(AXIdentifier="subtitle").AXValue
        except ui.UIErr:
            pass

        return Notification(
            identifier=identifier,
            subrole=group.AXSubrole,
            app_name=group.AXDescription,
            stacking_identifier=group.AXStackingIdentifier,
            title=title,
            subtitle=subtitle,
            body=body,
            actions=group_actions,
        )

    @staticmethod
    def notifications_in_window(window):
        notifications = []

        # macOS Sequoia uses AXButton, previous versions use AXGroup.
        for group in list(window.children.find(AXRole="AXGroup")) + list(
            window.children.find(AXRole="AXButton")
        ):
            if not (identifier := Notification.group_identifier(group)):
                continue

            notification = Notification.from_group(group, identifier)
            notifications.append(notification)

        return notifications


MONITOR = None

ctx = Context()
ctx.matches = r"""
os: mac
"""

ctx.lists["user.notification_actions"] = {}
ctx.lists["user.notification_apps"] = {}


@ctx.action_class("user")
class UserActions:
    def notification_action(index: int, action: str) -> bool:
        return MONITOR.perform_action(action, index=index)

    def notification_app_action(app_name: str, action: str) -> bool:
        return MONITOR.perform_action(action, app_name=app_name)

    def notification_show_actions(index: int):
        MONITOR.show_actions(index)

    def notifications_update():
        MONITOR.update_notifications()

    def notification_center():
        cc = ui.apps(bundle="com.apple.controlcenter")[0]
        cc.element.children.find_one(AXRole="AXMenuBar", max_depth=0).children.find_one(
            AXRole="AXMenuBarItem",
            AXSubrole="AXMenuExtra",
            AXIdentifier="com.apple.menuextra.clock",
            max_depth=0,
        ).perform("AXPress")


@imgui.open()
def gui_actions(gui: imgui.GUI):
    global notification_actions

    gui.text("Notification actions")
    gui.text("Say “note <notification number> <action>”")
    gui.line()
    for notification in notification_actions:
        gui.text(notification)
    gui.spacer()
    if gui.button("Close (say “note actions”)"):
        actions.user.notification_show_actions(-1)


class NotificationMonitor:
    __slots__ = (
        "pid",
        "notifications",
    )

    def __init__(self, app: ui.App):
        self.pid = app.pid
        self.notifications = []

        ui.register("win_open", self.win_open)
        ui.register("win_close", self.win_close)
        ui.register("app_close", self.app_closed)

        self.update_notifications()

    def win_open(self, window):
        if not window.app.pid == self.pid:
            return

        notifications = Notification.notifications_in_window(window)
        self.update_notifications(adding=notifications)

    def notification_groups(self):
        ncui = ui.apps(pid=self.pid)[0]
        for window in ncui.windows():
            # macOS Sequoia uses AXButton, previous versions use AXGroup.
            for group in list(window.children.find(AXRole="AXGroup")) + list(
                window.children.find(AXRole="AXButton")
            ):
                if not (identifier := Notification.group_identifier(group)):
                    continue

                yield identifier, group

    def __getitem__(self, index):
        if index < 0 or index > len(self.notifications) - 1:
            app.notify(f"Unable to locate notification #{index + 1}", "Try again?")
            return None

        return self.notifications[index]

    def perform_action(
        self, action: str, index: Optional[int] = None, app_name: str = None
    ):
        self.update_notifications()

        cron.after("500ms", self.update_notifications)

        notification = None
        if index is not None:
            if (notification := self[index]) is None:
                return False

        elif app_name is not None:
            try:
                notification = next(
                    notification
                    for notification in self.notifications
                    if notification.app_name == app_name
                )
            except StopIteration:
                app.notify(
                    f"Unable to locate notification for {app_name}", "Try again?"
                )
                return False

        for identifier, group in self.notification_groups():
            if identifier != notification.identifier:
                continue

            if action not in notification.actions:
                # allow closing a notification stack like an individual notification
                if action == "close" and "clear all" in notification.actions:
                    action = "clear all"
                else:
                    app.notify(f"No such action “{action}”", "Try again?")
                    return False

            group.perform(notification.actions[action])
            return True

        app.notify("Unable to locate notification", "Try again?")
        return False

    def show_actions(self, index: int):
        global notification_actions

        if gui_actions.showing:
            gui_actions.hide()

        if index == -1:
            return

        self.update_notifications()

        if (notification := self[index]) is None:
            return

        for identifier, group in self.notification_groups():
            if identifier != notification.identifier:
                continue

            notification_actions = set(notification.actions.keys())

            frame = group.AXFrame
            break
        else:
            return

        gui_actions.x = frame.left - 300
        gui_actions.y = frame.top
        gui_actions.show()

    def update_notifications(self, adding=[]):
        if gui_actions.showing:
            gui_actions.hide()

        if adding:
            self.notifications += adding

        notifications = {}
        for identifier, group in self.notification_groups():
            y = group.AXPosition.y

            try:
                notifications[y] = self.notifications[
                    self.notifications.index(Notification(identifier=identifier))
                ]
            except ValueError:
                notifications[y] = Notification.from_group(group, identifier)

        # groups may be not be returned in order of increasing y
        notifications = dict(sorted(notifications.items()))

        self.notifications = list(notifications.values())
        if notifications:
            debug_print("notifications", notifications)

        notification_actions = set()
        notification_apps = set()

        for notification in self.notifications:
            notification_actions.update(notification.actions.keys())
            notification_apps.add(notification.app_name)

        notification_actions = list(notification_actions)
        # XXX(nriley) create_spoken_forms_from_list doesn't handle apostrophes correctly
        # https://github.com/knausj85/knausj_talon/issues/780
        apostrophe_words = {
            word.replace("'", " "): word
            for word in chain.from_iterable(
                action.split() for action in notification_actions
            )
            if "'" in word
        }
        words_to_exclude = [word.split(" ")[0] for word in apostrophe_words]
        notification_actions = actions.user.create_spoken_forms_from_list(
            notification_actions, words_to_exclude=words_to_exclude
        )
        if apostrophe_words:
            notification_actions = {
                spoken_form.replace(mangled_word, word): action
                for mangled_word, word in apostrophe_words.items()
                for spoken_form, action in notification_actions.items()
                if "apostrophe" not in spoken_form
            }
        if notification_actions:
            debug_print("actions", notification_actions)

        if "close" not in notification_actions and "clear all" in notification_actions:
            # allow closing a notification stack like an individual notification
            notification_actions["close"] = "clear all"
        ctx.lists["user.notification_actions"] = notification_actions

        # XXX(nriley) use app name overrides from knausj?
        notification_apps = actions.user.create_spoken_forms_from_list(
            notification_apps
        )
        ctx.lists["user.notification_apps"] = notification_apps
        if notification_apps:
            debug_print("apps", notification_apps)

    def win_close(self, window):
        if not window.app.pid == self.pid:
            return

        self.update_notifications()

    def app_closed(self, app):
        if app.pid == self.pid:
            ui.unregister("app_close", self.app_closed)


def app_launched(app):
    global MONITOR

    if not app.bundle == "com.apple.notificationcenterui":
        return

    MONITOR = NotificationMonitor(app)


def monitor():
    global MONITOR

    apps = ui.apps(bundle="com.apple.notificationcenterui")
    if apps:
        MONITOR = NotificationMonitor(apps[0])

    ui.register("app_launch", app_launched)


app.register("ready", monitor)
