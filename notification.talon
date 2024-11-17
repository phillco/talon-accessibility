os: mac
-
^(note | notification) <number_small> {user.notification_actions}$:
    user.notification_action(number_small - 1, notification_actions)

^(note | notification) <number_small> actions$:
    user.notification_show_actions(number_small - 1)

^(note | notification) actions$: user.notification_show_actions(-1)

^(note | notification) {user.notification_actions} <number_small>$:
    user.notification_action(number_small - 1, notification_actions)

^(note | notification) {user.notification_actions} {user.notification_apps}$:
    user.notification_app_action(notification_apps, notification_actions)

^(note | notification) {user.notification_apps} {user.notification_actions}$:
    user.notification_app_action(notification_apps, notification_actions)

^(note | notification) center$: user.notification_center()
