"""Textual dashboard application."""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from github_checker.config import load_config
from github_checker.github import fetch_all
from github_checker.models import RepoState

COLUMNS = ("Repo", "PRs", "Bot", "Branches", "Alerts", "Copilot", "Updated")

_COPILOT_STATE_LABELS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes",
    "COMMENTED": "commented",
}


def _count(n: int) -> str:
    return "100+" if n >= 100 else str(n)


def repo_row(state: RepoState) -> tuple[str, str, str, str, str, str, str]:
    """Build one table row for a repository."""
    if state.error:
        return (state.name, "-", "-", "-", "-", "-", "error")
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
        self._config = load_config(self._config_path)
        self.run_worker(self._refresh(), exclusive=True)

    async def _refresh(self) -> None:
        self.sub_title = "refreshing…"
        states = await fetch_all(self._config.repos)
        self.apply_states(states)
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
