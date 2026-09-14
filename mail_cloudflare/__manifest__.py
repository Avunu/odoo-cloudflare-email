# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
{
    "name": "Cloudflare Email Transport",
    "summary": "Send and receive email via Cloudflare Email Sending and Email Workers",
    # Split so release-please's generic updater (which replaces the first
    # semver on the marker line) bumps only the module part; ast.literal_eval
    # folds the implicit concatenation back into "18.0.1.0.0". Both parts
    # carry a trailing comment on purpose: ruff's formatter joins an implicit
    # concatenation that fits on one line unless every part has one.
    "version": (
        "18.0."  # Odoo series
        "1.0.0"  # x-release-please-version
    ),
    "category": "Discuss",
    "author": "Avunu LLC",
    "website": "https://avu.nu",
    "license": "AGPL-3",
    "installable": True,
    "depends": ["mail"],
    "data": [],
}
