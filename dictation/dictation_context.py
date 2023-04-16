import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from talon import Context, Module, actions, ui

try:
    from talon.ui import Element
except ImportError:
    Element = type(None)
from talon.types import Span

ctx = Context()
ctx.matches = "os: mac"

mod = Module()
setting_accessibility_dictation = mod.setting(
    "accessibility_dictation",
    type=bool,
    default=False,
    desc="Use accessibility APIs to implement context aware dictation.",
)

# Default number of characters to use to acquire context. Somewhat arbitrary.
# The current dictation formatter doesn't need very many, but that could change in the future.
DEFAULT_CONTEXT_CHARACTERS = 30


@dataclass
class AccessibilityContext:
    """Records the context needed for dictation"""

    content: str
    selection: Span

    def left_context(self, num_chars: int = DEFAULT_CONTEXT_CHARACTERS) -> str:
        """Returns `num_chars`' worth of context to the left of the cursor"""
        start = max(0, self.selection.left - num_chars)
        return self.content[start : self.selection.left]

    def right_context(self, num_chars: int = DEFAULT_CONTEXT_CHARACTERS) -> str:
        """Returns `num_chars`' worth of context to the right of the cursor"""
        end = min(self.selection.right + num_chars, len(self.content))
        return self.content[self.selection.right : end]


@mod.action_class
class ModActions:
    def accessibility_dictation_enabled() -> bool:
        """Returns whether accessibility dictation should be used"""
        # NB: for access within other files, since they can't import `setting_accessibility_dictation`
        return setting_accessibility_dictation.get()

    def dictation_current_element() -> Element:
        """Returns the accessibility element that should be used for dictation (i.e. the current input textbox).

        This is almost always the focused (current) element, however, this action
        exists so that context can overwrite it, for applications with strange behavior.
        """
        return ui.focused_element()

    def accessibility_adjust_context_for_application(
        el: Element, context: AccessibilityContext
    ) -> AccessibilityContext:
        """Hook for applications to override the reported buffer contents/cursor location.

        Sometimes the accessibility context reported by the application is wrong, but fixable in predictable ways (this is most common in Electron apps). This method can be overwritten in those applications to do so.
        """

        # TODO(pcohen): it's a it strange to have both this and dictation_current_element;
        # possibly refactor.
        return context

    def accessibility_create_dictation_context(
        el: Element,
    ) -> Optional[AccessibilityContext]:
        """Creates a `AccessibilityContext` representing the state of the input buffer for dictation mode"""
        if not actions.user.accessibility_dictation_enabled():
            return None

        if not el or not el.attrs:
            # No accessibility support.
            return None

        # NOTE(pcohen): In Microsoft apps (Word, OneNote), selection will be none when the cursor
        # is that the start of the input buffer.
        # TODO(pcohen): this should probably be an app-specific `accessibility_adjust_context_for_application`
        selection = el.get("AXSelectedTextRange")
        if selection is None:
            selection = Span(0, 0)

        context = AccessibilityContext(content=el.get("AXValue"), selection=selection)

        # Support application-specific overrides:
        context = actions.user.accessibility_adjust_context_for_application(el, context)

        # If we don't appear to have any accessibility information, don't use it.
        if context.content is None or context.selection is None:
            return None

        return context


# TODO(pcohen): relocate this
class Colors(Enum):
    RESET = "\033[0m"
    RED = "\033[31m"
    YELLOW = "\033[33m"


@ctx.action_class("self")
class Actions:
    """Wires this into the knausj dictation formatter"""

    def dictation_peek(left, right):
        before, after = None, None

        try:
            if not setting_accessibility_dictation.get():
                return actions.next(left, right)

            el = actions.user.dictation_current_element()
            context = actions.user.accessibility_create_dictation_context(el)
            if context is None:
                print(
                    f"{Colors.YELLOW.value}Accessibility not available for context-aware dictation{Colors.RESET.value}; falling back to cursor method"
                )
                return actions.next(left, right)

            if left:
                before = context.left_context()
            if right:
                after = context.right_context()
        except Exception as e:
            print(
                f"{Colors.RED.value}{type(e).__name__} while querying accessibility for context-aware dictation:{Colors.RESET.value} '{e}':"
            )
            traceback.print_exc()

            # Fallback to the original (keystrokes) knausj method.
            return actions.next(left, right)

        return before, after
