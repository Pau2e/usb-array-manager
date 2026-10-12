from html import escape

from PySide6.QtWidgets import QLabel, QPlainTextEdit, QTabWidget, QTextBrowser, QVBoxLayout, QWidget

from usb_array_manager.services.production_planning import ProductionPlan, VHDX_OVERHEAD, gib


class ProductionReadinessTab(QWidget):
    """Presentation only; the backend owns validation evidence and sizing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = QLabel("Production deployment: BLOCKED")
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(False)
        self.commands = QPlainTextEdit()
        self.commands.setReadOnly(True)
        self.commands.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.details, "Readiness / layout")
        self.tabs.addTab(self.commands, "Commands / startup preview")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.status)
        layout.addWidget(self.tabs, 1)

    def set_plan(self, plan: ProductionPlan) -> None:
        self.status.setText(f"Production deployment: {plan.status} — PREVIEW ONLY")
        color = "#b00020" if plan.status == "BLOCKED" else "#9a6700" if plan.status == "WARNING" else "#137333"
        self.status.setStyleSheet(f"color: {color}; font-weight: 700;")
        sections = ["<h3>Production readiness</h3><table cellpadding='4' width='100%'>"]
        needs_migration = any(not member.filesystem_ready for member in plan.members)
        sizing_note = (
            "PRE-FORMAT ESTIMATE; recalculate after migration"
            if needs_migration else "estimate; revalidate before creation"
        )
        for check in plan.checks:
            sections.append(f"<tr><td>{escape(check.name)}</td><td><b>{escape(check.status)}</b></td>"
                            f"<td>{escape(check.detail)}</td></tr>")
        sections.append("</table><h3>Host filesystem / deterministic file layout</h3>")
        sections.append("<table cellpadding='4' width='100%'><tr><th>Slot</th><th>Current filesystem</th>"
                        "<th>Host capacity</th><th>Free</th><th>Compatible</th></tr>")
        for member in plan.members:
            volume = member.volume
            sections.append(f"<tr><td>{member.slot}</td><td>{escape(volume.filesystem or 'Unknown') if volume else 'Unknown'}</td>"
                            f"<td>{gib(volume.capacity_bytes) if volume else 'Unknown'}</td>"
                            f"<td>{gib(volume.free_bytes) if volume else 'Unknown'}</td>"
                            f"<td>{'READY' if member.filesystem_ready else 'BLOCKED'}</td></tr>")
        sections.append("</table>")
        for member in plan.members:
            sections.append(f"<p><b>Slot {member.slot}</b> — current drive letter {escape(member.letter)}<br>"
                            f"Persistent host volume (authoritative, not the drive letter): {escape(member.volume.identity or 'Unknown') if member.path and member.volume else 'Unproven'}<br>"
                            f"Planned file: {escape(member.path or 'BLOCKED — unproven volume identity')}<br>"
                            f"Logical size: {gib(plan.logical_bytes)} — Dynamically expanding ({sizing_note})<br>"
                            f"Required action: {escape(member.action)}</p>")
        sections.append("<h3>Capacity chain — estimates, not allocation guarantees</h3>"
                        "<p>After FAT32 → NTFS migration, rediscover Volume GUIDs and recalculate capacity, "
                        "free space, reserves and VHDX sizes. Never reuse pre-format sizing blindly.</p>"
                        f"<p>Raw physical USB: {gib(plan.physical_bytes)}<br>"
                        f"→ Host reserve: {gib(plan.reserve_bytes)} per host (plus {gib(VHDX_OVERHEAD)} VHDX / filesystem overhead allowance each)<br>"
                        f"→ Four VHDXs: 4 × {gib(plan.logical_bytes)}<br>"
                        f"→ Storage Spaces raw virtual: {gib(plan.virtual_raw_bytes)}<br>"
                        f"→ Two-way mirrored theoretical: {gib(plan.mirrored_bytes)}<br>"
                        f"→ Approximate formatted usable: {gib(plan.formatted_bytes)} "
                        "(1% pool/allocation margin, then 1% NTFS allowance; actual capacity may differ).</p>"
                        "<p>Two-way mirror / RAID10-like layout is not true RAID10. "
                        "Storage Spaces controls placement; saved mirror pairs are not enforced. "
                        "One member failure is guaranteed tolerable; two are not guaranteed. "
                        "Final filesystem: NTFS. Mount point: not configured.</p>")
        self.details.setHtml("".join(sections))
        self.commands.setPlainText(plan.preview)
