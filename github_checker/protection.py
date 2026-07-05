"""Ruleset management screen."""

from collections.abc import Coroutine
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Label, OptionList, Static

from github_checker.github import (
    GhError,
    copy_ruleset,
    delete_ruleset,
    get_ruleset,
    list_rulesets,
    set_ruleset_enforcement,
)
from github_checker.models import RulesetDetails, RulesetInfo

_RULE_LABELS = {
    "deletion": "запрет удаления",
    "non_fast_forward": "запрет force-push",
    "update": "запрет прямых пушей",
    "pull_request": "только через PR",
    "required_status_checks": "обязательные проверки CI",
    "required_signatures": "подписанные коммиты",
}


def protection_details_text(details: RulesetDetails) -> str:
    """Plain-text rendering of one ruleset for the details panel."""
    include = [
        "default" if ref == "~DEFAULT_BRANCH" else ref for ref in details.include
    ]
    lines = [details.name, f"enforcement: {details.enforcement}", ""]
    lines.append("Ветки: " + (", ".join(include) if include else "(не задано)"))
    if details.exclude:
        lines.append("Исключения: " + ", ".join(details.exclude))
    lines += ["", "Правила:"]
    if details.rules:
        lines += [f"  {_RULE_LABELS.get(r, r)}" for r in details.rules]
    else:
        lines.append("  (нет)")
    lines += ["", "Bypass:"]
    lines.append("  " + ("; ".join(details.bypass) if details.bypass else "(никто)"))
    return "\n".join(lines)


class RepoPickerScreen(ModalScreen[str | None]):
    """Choose a target repository for a ruleset copy."""

    CSS = """
    RepoPickerScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $accent; padding: 1 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, repos: list[str]) -> None:
        super().__init__()
        self._repos = repos

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Копировать в репозиторий:")
            yield OptionList(*self._repos, id="repo-list")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProtectionScreen(Screen[list[RulesetInfo] | None]):
    """List, toggle, copy and delete rulesets of one repository."""

    CSS = """
    #rulesets { height: 1fr; }
    #protection-details-scroll { height: 1fr; border-top: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("e", "toggle_enforcement", "Вкл/выкл"),
        ("c", "copy", "Копировать"),
        ("x", "delete", "Удалить"),
        ("escape", "close", "Назад"),
        ("q", "close", "Назад"),
    ]

    def __init__(self, repo: str, all_repos: list[str]) -> None:
        super().__init__()
        self._repo = repo
        self._other_repos = [r for r in all_repos if r != repo]
        self._infos: list[RulesetInfo] | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="rulesets", cursor_type="row")
        with VerticalScroll(id="protection-details-scroll"):
            yield Static("", id="protection-details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self._repo
        table = self.query_one(DataTable)
        table.add_columns("Name", "Enforcement", "Target")
        self.run_worker(self._reload(), exclusive=True)

    async def _reload(self) -> None:
        try:
            infos = await list_rulesets(self._repo)
        except GhError as err:
            self.notify(_one_line(err.message), severity="error")
            return
        except Exception as err:
            self.notify(f"{type(err).__name__}: {err}"[:120], severity="error")
            return
        self._infos = infos
        table = self.query_one(DataTable)
        table.clear()
        for info in self._infos:
            table.add_row(info.name, info.enforcement, info.target, key=str(info.id))

    def _selected_info(self) -> RulesetInfo | None:
        if self._infos is None:
            return None
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        row = table.cursor_coordinate.row
        if 0 <= row < len(self._infos):
            return self._infos[row]
        return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        info = self._selected_info()
        if info is not None:
            self.run_worker(
                self._load_details(info.id), exclusive=True, group="details"
            )

    async def _load_details(self, ruleset_id: int) -> None:
        panel = self.query_one("#protection-details", Static)
        try:
            details = await get_ruleset(self._repo, ruleset_id)
        except GhError as err:
            panel.update(f"Не удалось загрузить детали: {_one_line(err.message)}")
            return
        except Exception as err:
            panel.update(f"Не удалось загрузить детали: {type(err).__name__}: {err}")
            return
        panel.update(protection_details_text(details))

    def _run_op(self, coro: Coroutine[Any, Any, None]) -> None:
        if self._busy:
            self.notify("Операция уже выполняется", severity="warning")
            coro.close()
            return
        self._busy = True
        self.run_worker(self._guarded(coro), group="ops")

    async def _guarded(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except GhError as err:
            self.notify(_one_line(err.message), severity="error")
        except Exception as err:
            self.notify(f"{type(err).__name__}: {err}"[:120], severity="error")
        finally:
            self._busy = False

    def action_toggle_enforcement(self) -> None:
        info = self._selected_info()
        if info is None:
            return

        async def op() -> None:
            new = "disabled" if info.enforcement == "active" else "active"
            await set_ruleset_enforcement(self._repo, info.id, new)
            self.notify(f"{info.name}: {new}")
            await self._reload()

        self._run_op(op())

    def action_copy(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        if not self._other_repos:
            self.notify("Нет других репозиториев в конфиге", severity="warning")
            return

        def handle_result(target: str | None) -> None:
            if not target:
                return

            async def op() -> None:
                await copy_ruleset(self._repo, info.id, target)
                self.notify(f"{info.name} скопирован в {target}")

            self._run_op(op())

        self.app.push_screen(RepoPickerScreen(self._other_repos), handle_result)

    def action_delete(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        from github_checker.app import ConfirmScreen

        def handle_result(confirmed: bool | None) -> None:
            if not confirmed:
                return

            async def op() -> None:
                await delete_ruleset(self._repo, info.id)
                self.notify(f"{info.name} удалён")
                await self._reload()

            self._run_op(op())

        self.app.push_screen(
            ConfirmScreen(f"Удалить ruleset «{info.name}»?"), handle_result
        )

    def action_close(self) -> None:
        self.dismiss(self._infos)


def _one_line(text: str) -> str:
    return " ".join(text.split())[:120]
