from dataclasses import dataclass, field
from itertools import chain
from typing import Optional

from talon import Context, Module, actions, app, imgui, settings, speech_system, ui

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
    def identifier(notification):
        identifier = getattr(notification, "AXIdentifier", None)

        if identifier is None or not str.isdigit(identifier):
            return None

        return int(identifier)

    @staticmethod
    def from_button(button, identifier):
        # XXX(nriley) better handle AXNotificationCenterBannerStack
        button_actions = button.actions
        if "AXScrollToVisible" in button_actions:
            del button_actions["AXScrollToVisible"]  # not useful
        # XXX(nriley) create_spoken_forms_from_list doesn't handle apostrophes correctly
        # https://github.com/talonhub/community/issues/780
        button_actions = {
            name.lower().replace("’", "'"): action
            for action, name in button_actions.items()
        }

        title = body = subtitle = None

        try:
            title = button.children.find_one(AXIdentifier="title").AXValue
        except ui.UIErr:
            pass

        try:
            body = button.children.find_one(AXIdentifier="body").AXValue
        except ui.UIErr:
            pass

        try:
            subtitle = button.children.find_one(AXIdentifier="subtitle").AXValue
        except ui.UIErr:
            pass

        return Notification(
            identifier=identifier,
            subrole=button.AXSubrole,
            app_name=button.AXDescription,
            stacking_identifier=button.AXStackingIdentifier,
            title=title,
            subtitle=subtitle,
            body=body,
            actions=button_actions,
        )


MONITOR = None

ctx = Context()
ctx.matches = r"""
os: mac
"""


@ctx.dynamic_list("user.notification_actions")
def notification_actions(phrase):
    return MONITOR.actions


@ctx.dynamic_list("user.notification_apps")
def notification_apps(phrase):
    return MONITOR.apps


@ctx.action_class("user")
class UserActions:
    def notification_action(index: int, action: str) -> bool:
        return MONITOR.perform_action(action, index=index)

    def notification_app_action(app_name: str, action: str) -> bool:
        return MONITOR.perform_action(action, app_name=app_name)

    def notification_show_actions(index: int):
        MONITOR.show_actions(index)

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
    gui.text("Notification actions")
    gui.text("Say “note <notification number> <action>”")
    gui.line()
    for notification in MONITOR.actions_for_notification:
        gui.text(notification)
    gui.spacer()
    if gui.button("Close (say “note actions”)"):
        actions.user.notification_show_actions(-1)


class NotificationMonitor:
    __slots__ = (
        "pid",
        "actions_for_notification",
        "_notifications",
        "_actions",
        "_apps",
    )

    def __init__(self, app: ui.App):
        self.pid = app.pid

        ui.register("win_open", self.win_open)
        ui.register("win_close", self.win_close)
        ui.register("app_close", self.app_closed)

        self.hide_actions()

    def win_open(self, window):
        if not window.app.pid == self.pid:
            return

        self.hide_actions()

    def notification_buttons(self):
        ncui = ui.apps(pid=self.pid)[0]
        for window in ncui.windows():
            try:
                button_list = window.children.find_one(AXSubrole="AXOpaqueProviderList")
            except ui.UIErr:
                continue

            for child in button_list.children:
                if getattr(child, "AXSubrole", None) not in (
                    "AXNotificationCenterAlert",
                    "AXNotificationCenterBanner",
                    "AXNotificationCenterBannerStack",
                ):
                    continue
                if not (identifier := Notification.identifier(child)):
                    continue

                yield identifier, child

    def __getitem__(self, index):
        notifications = self.notifications

        if index < 0 or index > len(notifications) - 1:
            app.notify(f"Unable to locate notification #{index + 1}", "Try again?")
            return None

        return notifications[index]

    def perform_action(
        self, action: str, index: Optional[int] = None, app_name: str = None
    ):
        notification = None
        if index is not None:
            if (notification := self[index]) is None:
                return False

        elif app_name is not None:
            try:
                notification = next(
                    notification
                    for notification in self._notifications
                    if notification.app_name == app_name
                )
            except StopIteration:
                app.notify(
                    f"Unable to locate notification for {app_name}", "Try again?"
                )
                return False

        for identifier, button in self.notification_buttons():
            if identifier != notification.identifier:
                continue

            if action not in notification.actions:
                # allow closing a notification stack like an individual notification
                if action == "close" and "clear all" in notification.actions:
                    action = "clear all"
                else:
                    app.notify(f"No such action “{action}”", "Try again?")
                    return False

            button.perform(notification.actions[action])
            return True

        app.notify("Unable to locate notification", "Try again?")
        return False

    def show_actions(self, index: int):
        self.hide_actions()

        if index == -1:
            return

        if (notification := self[index]) is None:
            return

        for identifier, button in self.notification_buttons():
            if identifier != notification.identifier:
                continue

            # XXX(nriley) actions are returned out of (menu) order
            # XXX(nriley) sorting them is better than nothing
            self.actions_for_notification = sorted(notification.actions.keys())

            frame = button.AXFrame
            break
        else:
            return

        gui_actions.x = frame.left - 300
        gui_actions.y = frame.top
        gui_actions.show()

    def hide_actions(self):
        if gui_actions.showing:
            gui_actions.hide()

            del self.actions_for_notification

    @property
    def actions(self):
        self.update()
        return self._actions

    @property
    def apps(self):
        self.update()
        return self._apps

    @property
    def notifications(self):
        self.update()
        return self._notifications

    def update(self):
        if hasattr(self, "_actions"):
            return

        self.hide_actions()

        notifications = {}
        for identifier, button in self.notification_buttons():
            y = button.AXPosition.y
            notifications[y] = Notification.from_button(button, identifier)

        # notification buttons may be not be returned in order of increasing y
        notifications = dict(sorted(notifications.items()))
        if notifications:
            debug_print("notifications", notifications)
        self._notifications = notifications = list(notifications.values())

        notification_actions = set()
        notification_apps = set()

        for notification in notifications:
            notification_actions.update(notification.actions.keys())
            notification_apps.add(notification.app_name)

        notification_actions = list(notification_actions)
        # XXX(nriley) create_spoken_forms_from_list doesn't handle apostrophes correctly
        # https://github.com/talonhub/community/issues/780
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
        if "actions" in notification_actions:
            # avoid conflict with "note [...] actions"
            del notification_actions["actions"]
        if notification_actions:
            debug_print("actions", notification_actions)

        if "close" not in notification_actions and "clear all" in notification_actions:
            # allow closing a notification stack like an individual notification
            notification_actions["close"] = "clear all"
        self._actions = notification_actions

        # XXX(nriley) use app name overrides from community?
        notification_apps = actions.user.create_spoken_forms_from_list(
            notification_apps
        )
        if notification_apps:
            debug_print("apps", notification_apps)
        self._apps = notification_apps

    def flush_notification_cache(self):
        if not hasattr(self, "_actions"):
            return

        del self._apps
        del self._actions
        del self._notifications

    def win_close(self, window):
        if not window.app.pid == self.pid:
            return

        self.hide_actions()

    def app_closed(self, app):
        if app.pid == self.pid:
            ui.unregister("app_close", self.app_closed)


def app_launched(app):
    global MONITOR

    if not app.bundle == "com.apple.notificationcenterui":
        return

    MONITOR = NotificationMonitor(app)


def post_phrase(_):
    if MONITOR:
        MONITOR.flush_notification_cache()


def on_ready():
    global MONITOR

    apps = ui.apps(bundle="com.apple.notificationcenterui")
    if apps:
        MONITOR = NotificationMonitor(apps[0])

    ui.register("app_launch", app_launched)

    speech_system.register("post:phrase", post_phrase)


app.register("ready", on_ready)
