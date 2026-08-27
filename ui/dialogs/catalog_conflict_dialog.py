"""Per-occurrence duplicate resolution for music-service artist catalogs."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, PrimaryPushButton, PushButton, SubtitleLabel

from core.artist_catalog import CatalogDuplicateGroup
from ui.dialogs.styled_dialog import StyledDialog, make_footer, set_button_role
from ui.i18n import t


def _category_name(track: dict) -> str:
    section = str(track.get("catalog_section") or "")
    return t(f"artist_section_{section}") if section else str(track.get("category") or "")


def _release_key(track: dict) -> str:
    return str(
        track.get("source_release_id")
        or track.get("release_id")
        or track.get("album")
        or ""
    )


class _OccurrenceRow(QFrame):
    def __init__(self, index: int, track: dict, parent=None) -> None:
        super().__init__(parent)
        self.index = index
        self.track = track
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        category = _category_name(track)
        album = str(track.get("album") or "")
        position = int(track.get("album_index") or 0)
        location = category
        if album:
            location += f" · {album}"
        if position:
            location += f" · #{position}"
        self.checkbox = QCheckBox(location)
        self.checkbox.setChecked(True)
        self.checkbox.setAccessibleName(location)
        layout.addWidget(self.checkbox, 1)


class _CatalogDuplicateCard(QFrame):
    def __init__(self, group: CatalogDuplicateGroup, tracks: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.group = group
        self.rows = [_OccurrenceRow(index, tracks[index]) for index in group.indices]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        header = QHBoxLayout()
        title = BodyLabel(group.title)
        title.setWordWrap(True)
        confidence = CaptionLabel(t(f"catalog_duplicate_{group.confidence}"))
        header.addWidget(QLabel("⚠"))
        header.addWidget(title, 1)
        header.addWidget(confidence)
        layout.addLayout(header)
        choices = QHBoxLayout()
        categories = sorted({
            str(row.track.get("catalog_section") or "")
            for row in self.rows if row.track.get("catalog_section")
        })
        if len(categories) > 1:
            for category in categories:
                button = PushButton(t("catalog_conflict_only", category=t(
                    f"artist_section_{category}",
                )))
                set_button_role(button, "secondary")
                button.clicked.connect(
                    lambda _checked=False, key=category: self.select_only_category(key)
                )
                choices.addWidget(button)
        releases: dict[str, str] = {}
        for row in self.rows:
            release_key = _release_key(row.track)
            if release_key:
                releases.setdefault(release_key, str(row.track.get("album") or release_key))
        if len(categories) == 1 and len(releases) > 1:
            for release_key, release_label in releases.items():
                button = PushButton(t(
                    "catalog_conflict_only_release", release=release_label,
                ))
                set_button_role(button, "secondary")
                button.clicked.connect(
                    lambda _checked=False, key=release_key: self.select_only_release(key)
                )
                choices.addWidget(button)
        choices.addStretch()
        layout.addLayout(choices)
        for row in self.rows:
            layout.addWidget(row)

    def selected_indices(self) -> set[int]:
        return {row.index for row in self.rows if row.checkbox.isChecked()}

    def select_only_category(self, category: str) -> None:
        for row in self.rows:
            row.checkbox.setChecked(str(row.track.get("catalog_section") or "") == category)

    def select_only_release(self, release_key: str) -> None:
        for row in self.rows:
            row.checkbox.setChecked(_release_key(row.track) == release_key)

    def set_all(self, checked: bool) -> None:
        for row in self.rows:
            row.checkbox.setChecked(checked)


class CatalogConflictDialog(StyledDialog):
    def __init__(
        self,
        groups: list[CatalogDuplicateGroup],
        tracks: list[dict],
        parent=None,
    ) -> None:
        super().__init__(parent, minimum_size=(620, 460), resize_to=(760, 620))
        self._groups = groups
        self._tracks = tracks
        self._cards: list[_CatalogDuplicateCard] = []
        self.decisions: dict[str, set[int]] = {}
        self._build()

    def _build(self) -> None:
        self.setWindowTitle(t("catalog_conflict_title"))
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        root.addWidget(SubtitleLabel(t("catalog_conflict_subtitle", n=len(self._groups))))
        desc = CaptionLabel(t("catalog_conflict_explanation"))
        desc.setWordWrap(True)
        root.addWidget(desc)

        toolbar = QHBoxLayout()
        keep_all = PushButton(t("catalog_conflict_keep_all"))
        clear_all = PushButton(t("catalog_conflict_clear_all"))
        keep_all.clicked.connect(lambda: self.set_all(True))
        clear_all.clicked.connect(lambda: self.set_all(False))
        toolbar.addWidget(keep_all)
        toolbar.addWidget(clear_all)
        toolbar.addStretch()
        root.addLayout(toolbar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        card_layout = QVBoxLayout(container)
        for group in self._groups:
            card = _CatalogDuplicateCard(group, self._tracks)
            self._cards.append(card)
            card_layout.addWidget(card)
        card_layout.addStretch()
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        cancel = PushButton(t("cancel_btn"))
        confirm = PrimaryPushButton(t("catalog_conflict_confirm"))
        set_button_role(cancel, "cancel")
        set_button_role(confirm, "primary", default=True)
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self._accept_decisions)
        root.addWidget(make_footer(cancel, confirm))

    def select_only_category(self, category: str) -> None:
        for card in self._cards:
            card.select_only_category(category)

    def set_all(self, checked: bool) -> None:
        for card in self._cards:
            card.set_all(checked)

    def _accept_decisions(self) -> None:
        self.decisions = {
            card.group.group_id: card.selected_indices() for card in self._cards
        }
        self.accept()
