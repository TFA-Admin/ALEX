# controller/views/data.py
"""Data — direct read/write view of db/memory.db.

Uses plain sqlite3 (not db.py's aiosqlite helpers) since this is a
generic, table-agnostic browser rather than the specific queries db.py
exposes; button-triggered, not on any hot path. Same DB file the live
ALEX process uses; sqlite handles the concurrent access fine for these
short, infrequent transactions."""
import sqlite3

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QMessageBox,
)
from PySide6.QtCore import Qt

from controller.common import DB_PATH, DB_BLOB_COLUMNS


class DataView(QWidget):
    NEW_ROW_MARKER = "(new)"

    def __init__(self, note):
        super().__init__()
        self.note = note
        self.db_current_table = None
        self.db_current_columns = []
        self.db_current_blob_cols = set()

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)

        top = QHBoxLayout()
        top.addWidget(QLabel("Table:"))

        self.db_table_selector = QComboBox()
        self.db_table_selector.currentTextChanged.connect(self.load_db_table_data)
        top.addWidget(self.db_table_selector)

        self.db_refresh_btn = QPushButton("🔄 Refresh")
        # 2026-09-20 (Craig: "I see no corrections table") — this was wired
        # to load_db_table_data(), which reloads the ROWS of whatever table
        # is already selected. The dropdown itself was filled once at
        # startup from sqlite_master and never again, so any table created
        # after the Controller launched was invisible until it restarted —
        # exactly what happens when ALEX adds one on her next boot. Refresh
        # now means refresh: the list, then the rows.
        self.db_refresh_btn.clicked.connect(self.load_db_tables)
        top.addWidget(self.db_refresh_btn)
        top.addStretch(1)
        layout.addLayout(top)

        self.db_table = QTableWidget()
        self.db_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.db_table)

        btns = QHBoxLayout()
        self.db_add_row_btn = QPushButton("➕ Add Row")
        self.db_add_row_btn.clicked.connect(self.add_db_row)
        self.db_delete_row_btn = QPushButton("🗑️ Delete Selected Row")
        self.db_delete_row_btn.clicked.connect(self.delete_db_row)
        self.db_save_btn = QPushButton("💾 Save Changes")
        self.db_save_btn.clicked.connect(self.save_db_changes)
        for b in [self.db_add_row_btn, self.db_delete_row_btn, self.db_save_btn]:
            btns.addWidget(b)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.setLayout(layout)

    def load_db_tables(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = [r[0] for r in cursor.fetchall()]
            conn.close()
        except Exception as e:
            self.note(f"⚠️ Failed to list database tables: {e}")
            return

        # Hold the current selection across the refresh — otherwise pressing
        # Refresh silently jumps back to the first table alphabetically,
        # which reads as the button having lost your place.
        previous = self.db_table_selector.currentText()

        self.db_table_selector.blockSignals(True)
        self.db_table_selector.clear()
        self.db_table_selector.addItems(tables)
        if previous in tables:
            self.db_table_selector.setCurrentText(previous)
        self.db_table_selector.blockSignals(False)

        self.load_db_table_data()

    def load_db_table_data(self):
        table = self.db_table_selector.currentText()
        if not table:
            self.db_table.setRowCount(0)
            self.db_table.setColumnCount(0)
            return

        try:
            conn = sqlite3.connect(DB_PATH)
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")]
            rows = conn.execute(f"SELECT rowid, * FROM '{table}'").fetchall()
            conn.close()
        except Exception as e:
            self.note(f"⚠️ Failed to load table '{table}': {e}")
            return

        self.db_current_table = table
        self.db_current_columns = columns
        blob_cols = {c for t, c in DB_BLOB_COLUMNS if t == table}
        self.db_current_blob_cols = blob_cols

        headers = ["rowid"] + columns
        self.db_table.setColumnCount(len(headers))
        self.db_table.setHorizontalHeaderLabels(headers)
        self.db_table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                col_name = headers[c]

                if col_name in blob_cols:
                    item = QTableWidgetItem(f"<{len(value) if value else 0} bytes>" if value is not None else "")
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                else:
                    item = QTableWidgetItem("" if value is None else str(value))

                if col_name == "rowid":
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)

                self.db_table.setItem(r, c, item)

    def add_db_row(self):
        if not self.db_current_table:
            return

        headers = ["rowid"] + self.db_current_columns
        row = self.db_table.rowCount()
        self.db_table.insertRow(row)

        for c, col_name in enumerate(headers):
            if col_name == "rowid":
                item = QTableWidgetItem(self.NEW_ROW_MARKER)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            elif col_name in self.db_current_blob_cols:
                item = QTableWidgetItem("")
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            else:
                item = QTableWidgetItem("")

            self.db_table.setItem(row, c, item)

        self.db_table.scrollToBottom()

    def delete_db_row(self):
        table = self.db_current_table
        if not table:
            return

        rows = sorted({idx.row() for idx in self.db_table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return

        real_rowids = [
            self.db_table.item(r, 0).text() for r in rows
            if self.db_table.item(r, 0).text() != self.NEW_ROW_MARKER
        ]

        if real_rowids:
            confirm = QMessageBox.question(
                self, "Delete Row(s)",
                f"Permanently delete {len(real_rowids)} row(s) from '{table}'? This cannot be undone.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if confirm != QMessageBox.Yes:
                return

            try:
                conn = sqlite3.connect(DB_PATH)
                for rowid in real_rowids:
                    conn.execute(f"DELETE FROM '{table}' WHERE rowid=?", (rowid,))
                conn.commit()
                conn.close()
                self.note(f"[SYSTEM] Deleted {len(real_rowids)} row(s) from '{table}' via Controller")
            except Exception as e:
                self.note(f"⚠️ Failed to delete row(s): {e}")

        # remove locally too (covers both real rows just deleted and any
        # not-yet-saved "(new)" rows the user wants to discard)
        for r in rows:
            self.db_table.removeRow(r)

    def save_db_changes(self):
        table = self.db_current_table
        if not table:
            return

        writable_cols = [c for c in self.db_current_columns if c not in self.db_current_blob_cols]
        inserted = updated = 0

        try:
            conn = sqlite3.connect(DB_PATH)

            for r in range(self.db_table.rowCount()):
                rowid_item = self.db_table.item(r, 0)
                rowid = rowid_item.text() if rowid_item else ""

                values = []
                for col_name in writable_cols:
                    c = (["rowid"] + self.db_current_columns).index(col_name)
                    cell = self.db_table.item(r, c)
                    values.append(cell.text() if cell else "")

                if rowid == self.NEW_ROW_MARKER:
                    placeholders = ",".join("?" for _ in writable_cols)
                    col_list = ",".join(f"'{c}'" for c in writable_cols)
                    conn.execute(
                        f"INSERT INTO '{table}' ({col_list}) VALUES ({placeholders})",
                        values
                    )
                    inserted += 1
                else:
                    set_clause = ",".join(f"'{c}'=?" for c in writable_cols)
                    conn.execute(
                        f"UPDATE '{table}' SET {set_clause} WHERE rowid=?",
                        values + [rowid]
                    )
                    updated += 1

            conn.commit()
            conn.close()
            self.note(f"[SYSTEM] Saved '{table}': {inserted} inserted, {updated} updated (via Controller)")
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", f"Failed to save changes to '{table}':\n{e}")
            return

        self.load_db_table_data()
