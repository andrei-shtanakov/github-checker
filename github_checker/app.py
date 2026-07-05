"""Textual dashboard application."""

import tomllib
from pathlib import Path

from pydantic import ValidationError
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from github_checker.config import add_repo, load_config, remove_repo
from github_checker.github import fetch_all
from github_checker.models import RepoState, RulesetInfo

COLUMNS = ("Repo", "PRs", "Bot", "Branches", "Alerts", "Rules", "Copilot", "Updated")

_COPILOT_STATE_LABELS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes",
    "COMMENTED": "commented",
}


def _count(n: int) -> str:
    return "100+" if n >= 100 else str(n)


def rules_cell(rulesets: list[RulesetInfo] | None) -> str:
    """Rules column value: ✓N active / offN present-but-off / - none / ? unknown."""
    if rulesets is None:
        return "?"
    active = sum(1 for r in rulesets if r.enforcement == "active")
    if active:
        return f"✓{active}"
    if rulesets:
        return f"off{len(rulesets)}"
    return "-"


def repo_row(state: RepoState) -> tuple[str, str, str, str, str, str, str, str]:
    """Build one table row for a repository."""
    if state.error:
        return (state.name, "-", "-", "-", "-", "-", "-", "error")
    bot = sum(1 for p in state.pulls if p.is_dependabot)
    with_copilot = sum(1 for p in state.pulls if p.copilot_review)
    alerts = "n/a" if state.alerts is None else _count(state.alerts)
    updated = state.updated_at.strftime("%H:%M:%S") if state.updated_at else "-"
    return (
        state.name,
        _count(len(state.pulls)),
        str(bot),
        _count(len(state.branches)),
        alerts,
        rules_cell(state.rulesets),
        f"{with_copilot}/{len(state.pulls)}",
        updated,
    )


def details_text(state: RepoState) -> str:
    """Plain-text details panel for one repository."""
    if state.error:
        return f"{state.name}\n\nERROR: {state.error}"
    lines = [state.name, "", "Pull requests:"]
    if not state.pulls:
        lines.append("  (none)")
    for pull in state.pulls:
        badges = ""
        if pull.is_dependabot:
            badges += " [dbot]"
        if pull.copilot_review:
            label = _COPILOT_STATE_LABELS.get(
                pull.copilot_review.state, pull.copilot_review.state.lower()
            )
            badges += f" [copilot: {label} ({pull.copilot_review.comment_count})]"
        lines.append(f"  #{pull.number} {pull.title} ({pull.author}){badges}")
    lines += ["", "Branches:"]
    if not state.branches:
        lines.append("  (none)")
    lines += [f"  {branch.name}" for branch in state.branches]
    return "\n".join(lines)


class AddRepoScreen(ModalScreen[str | None]):
    """Prompt for an owner/repo string."""

    CSS = """
    AddRepoScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $accent; padding: 1 2; }
    #dialog Horizontal { height: auto; align-horizontal: right; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Добавить репозиторий (owner/repo):")
            yield Input(placeholder="owner/repo", id="repo-input")
            with Horizontal():
                yield Button("Add", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            value = self.query_one("#repo-input", Input).value.strip()
            self.dismiss(value or None)
        else:
            self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no confirmation dialog."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $accent; padding: 1 2; }
    #dialog Horizontal { height: auto; align-horizontal: right; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal():
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class GithubCheckerApp(App[None]):
    """Dashboard showing the state of multiple GitHub repositories."""

    TITLE = "github-checker"
    CSS = """
    #table { width: 2fr; }
    #details-scroll { width: 1fr; border-left: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("a", "add_repo", "Add repo"),
        ("d", "remove_repo", "Remove repo"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self._config_path = config_path
        self._config = load_config(config_path)
        self._states: dict[str, RepoState] = {}
        self._selected: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="table", cursor_type="row")
            with VerticalScroll(id="details-scroll"):
                yield Static("", id="details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*COLUMNS)
        self.set_interval(self._config.refresh_seconds, self.action_refresh)
        self.action_refresh()

    def action_refresh(self) -> None:
        """Reload config from disk and refetch everything in the background."""
        try:
            self._config = load_config(self._config_path)
        except (tomllib.TOMLDecodeError, ValidationError) as err:
            self.notify(f"repos.toml не перечитан: {err}", severity="error")
        self.run_worker(self._refresh(), exclusive=True)

    async def _refresh(self) -> None:
        self.sub_title = "refreshing…"
        try:
            states = await fetch_all(self._config.repos)
            self.apply_states(states)
        finally:
            self.sub_title = ""

    def apply_states(self, states: list[RepoState]) -> None:
        """Replace table contents with freshly fetched states."""
        self._states = {s.name: s for s in states}
        table = self.query_one(DataTable)
        table.clear()
        for state in states:
            table.add_row(*repo_row(state), key=state.name)
        if self._selected not in self._states:
            self._selected = states[0].name if states else None
        if self._selected is not None:
            row_index = next(
                i for i, s in enumerate(states) if s.name == self._selected
            )
            table.move_cursor(row=row_index)
        self._show_details()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._selected = event.row_key.value
            self._show_details()

    def _show_details(self) -> None:
        details = self.query_one("#details", Static)
        state = self._states.get(self._selected) if self._selected else None
        if state is None:
            details.update("Нет репозиториев. Нажмите 'a', чтобы добавить.")
            return
        details.update(details_text(state))

    def action_add_repo(self) -> None:
        def handle_result(name: str | None) -> None:
            if not name:
                return
            if name in self._config.repos:
                self.notify(f"{name} уже в списке", severity="information")
                return
            try:
                self._config = add_repo(self._config_path, name)
            except ValidationError:
                self.notify(
                    f"Некорректное имя: {name!r} (нужно owner/repo)",
                    severity="error",
                )
                return
            self.action_refresh()

        self.push_screen(AddRepoScreen(), handle_result)

    def action_remove_repo(self) -> None:
        name = self._selected
        if name is None:
            return

        def handle_result(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self._config = remove_repo(self._config_path, name)
            self._selected = None
            self.action_refresh()

        self.push_screen(ConfirmScreen(f"Удалить {name}?"), handle_result)
