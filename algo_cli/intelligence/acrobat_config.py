"""B166, B167, B170, B177: Acrobat-derived config/dispatch/theme/telemetry patterns.

- B166: Locale-Sharded Resources with Default Fallback
- B167: Service Endpoint Dispatch Table
- B170: State-Based Declarative Theming
- B177: Telemetry Endpoint Config Separation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── B166: Locale-Sharded Resources with Default Fallback ─────────────


@dataclass
class LocaleResource:
    """A locale-specific resource (B166)."""
    locale: str
    resource_id: str
    content: Any = None
    fallback_used: bool = False


class LocaleResourceStore:
    """Locale-sharded resources with deterministic fallback (B166)."""

    def __init__(self, default_locale: str = "ENU") -> None:
        self._default = default_locale
        self._resources: dict[tuple[str, str], LocaleResource] = {}

    def register(self, locale: str, resource_id: str, content: Any) -> None:
        self._resources[(locale, resource_id)] = LocaleResource(
            locale=locale, resource_id=resource_id, content=content,
        )

    def get(self, locale: str, resource_id: str) -> LocaleResource | None:
        """Get a resource with fallback: exact locale -> language -> default."""
        # Try exact locale
        key = (locale, resource_id)
        if key in self._resources:
            return self._resources[key]

        # Try language prefix
        lang = locale.split("_")[0].upper()
        key = (lang, resource_id)
        if key in self._resources:
            r = self._resources[key]
            return LocaleResource(
                locale=lang, resource_id=resource_id,
                content=r.content, fallback_used=True,
            )

        # Try default
        key = (self._default, resource_id)
        if key in self._resources:
            r = self._resources[key]
            return LocaleResource(
                locale=self._default, resource_id=resource_id,
                content=r.content, fallback_used=True,
            )
        return None

    def available_locales(self) -> list[str]:
        return sorted({loc for loc, _ in self._resources.keys()})

    def has_resource(self, locale: str, resource_id: str) -> bool:
        return (locale, resource_id) in self._resources


# ── B167: Service Endpoint Dispatch Table ────────────────────────────


@dataclass
class ServiceEndpoint:
    """A single service endpoint (B167)."""
    operation: str
    url: str
    version: str = "v1"
    scope: str = "GM"  # "PR" (pre-release) or "GM" (general release)


@dataclass
class DispatchTable:
    """A dispatch table for service endpoints (B167)."""
    scope: str = "GM"
    endpoints: dict[str, ServiceEndpoint] = field(default_factory=dict)
    signature: str = ""

    def add(self, endpoint: ServiceEndpoint) -> None:
        self.endpoints[endpoint.operation] = endpoint

    def resolve(self, operation: str) -> ServiceEndpoint | None:
        """Resolve an operation to its endpoint."""
        return self.endpoints.get(operation)

    def operations(self) -> list[str]:
        return list(self.endpoints.keys())


class ServiceDispatchRegistry:
    """Registry of dispatch tables with scope variants (B167)."""

    def __init__(self) -> None:
        self._tables: dict[str, DispatchTable] = {}
        self._active_scope: str = "GM"

    def register_table(self, table: DispatchTable) -> None:
        self._tables[table.scope] = table

    def set_scope(self, scope: str) -> None:
        self._active_scope = scope

    def resolve(self, operation: str) -> ServiceEndpoint | None:
        """Resolve an operation using the active scope's dispatch table."""
        table = self._tables.get(self._active_scope)
        if not table:
            return None
        return table.resolve(operation)

    def available_scopes(self) -> list[str]:
        return list(self._tables.keys())

    def all_operations(self) -> list[str]:
        table = self._tables.get(self._active_scope)
        return table.operations() if table else []


# ── B170: State-Based Declarative Theming ─────────────────────────────


@dataclass
class ThemeStyle:
    """A style with state variants (B170)."""
    style_id: str
    states: dict[str, str] = field(default_factory=dict)  # state -> color

    def get(self, state: str = "normal") -> str | None:
        """Get the color for a state, falling back to normal."""
        return self.states.get(state, self.states.get("normal"))


@dataclass
class Theme:
    """A declarative theme (B170)."""
    theme_id: str
    title: str = ""
    icon_set: str = ""
    styles: dict[str, ThemeStyle] = field(default_factory=dict)

    def get_color(self, style_id: str, state: str = "normal") -> str | None:
        """Resolve a color for a style + state."""
        style = self.styles.get(style_id)
        if not style:
            return None
        return style.get(state)


class ThemeManager:
    """Manages themes with swap support (B170)."""

    def __init__(self) -> None:
        self._themes: dict[str, Theme] = {}
        self._active: str = ""

    def register(self, theme: Theme) -> None:
        self._themes[theme.theme_id] = theme
        if not self._active:
            self._active = theme.theme_id

    def set_active(self, theme_id: str) -> bool:
        if theme_id in self._themes:
            self._active = theme_id
            return True
        return False

    def get_active(self) -> Theme | None:
        return self._themes.get(self._active)

    def get_color(self, style_id: str, state: str = "normal") -> str | None:
        theme = self.get_active()
        if not theme:
            return None
        return theme.get_color(style_id, state)

    def all_themes(self) -> list[str]:
        return list(self._themes.keys())


def default_dark_theme() -> Theme:
    """Create a default dark theme from Acrobat's DarkTheme.acrotheme."""
    return Theme(
        theme_id="DarkTheme",
        title="Dark Theme",
        icon_set="DarkIcons",
        styles={
            "RegularBackground": ThemeStyle("RegularBackground", {"normal": "0x4D4D4D"}),
            "ProminentBackground": ThemeStyle("ProminentBackground", {"normal": "0x424242", "hover": "0x3A3A3A"}),
            "DocumentBackground": ThemeStyle("DocumentBackground", {"normal": "0x282828"}),
            "Border": ThemeStyle("Border", {"normal": "0x3A3A3A"}),
            "Icon": ThemeStyle("Icon", {
                "normal": "0xC2C2C2", "hover": "0x51AAFE",
                "active": "0x2175C8", "checked": "0x51AAFE",
            }),
            "RegularText": ThemeStyle("RegularText", {
                "normal": "0xEAEAEA", "hover": "0x51AAFE",
                "active": "0x2175C8", "checked": "0x51AAFE",
                "disabled": "0x9B9B9B",
            }),
            "Hyperlink": ThemeStyle("Hyperlink", {"normal": "0x70D4FF", "hover": "0x1DBBFF"}),
        },
    )


def default_light_theme() -> Theme:
    """Create a default light theme from Acrobat's LightTheme.acrotheme."""
    return Theme(
        theme_id="LightTheme",
        title="Light Theme",
        icon_set="LightIcons",
        styles={
            "RegularBackground": ThemeStyle("RegularBackground", {"normal": "0xFAFAFA"}),
            "ProminentBackground": ThemeStyle("ProminentBackground", {"normal": "0xEAEAEA", "hover": "0xE1E1E1"}),
            "DocumentBackground": ThemeStyle("DocumentBackground", {"normal": "0x999999"}),
            "Border": ThemeStyle("Border", {"normal": "0xCBCBCB"}),
            "Icon": ThemeStyle("Icon", {
                "normal": "0x6F6F6F", "hover": "0x2175C8",
                "active": "0x0E539B", "checked": "0x2175C8",
            }),
            "RegularText": ThemeStyle("RegularText", {
                "normal": "0x4D4D4D", "hover": "0x2175C8",
                "active": "0x0E539B", "checked": "0x2175C8",
                "disabled": "0x949494",
            }),
            "Hyperlink": ThemeStyle("Hyperlink", {"normal": "0x0252A3"}),
        },
    )


# ── B177: Telemetry Endpoint Config Separation ────────────────────────


@dataclass
class TelemetryEndpoint:
    """A telemetry endpoint (B177)."""
    name: str
    url: str
    port: int = 443


class TelemetryConfig:
    """Telemetry endpoint configuration loaded from a config file (B177)."""

    def __init__(self) -> None:
        self._endpoints: dict[str, TelemetryEndpoint] = {}
        self._enabled: bool = True

    def set_endpoint(self, name: str, url: str, port: int = 443) -> None:
        self._endpoints[name] = TelemetryEndpoint(name=name, url=url, port=port)

    def get_endpoint(self, name: str) -> TelemetryEndpoint | None:
        if not self._enabled:
            return None
        return self._endpoints.get(name)

    def load_from_lines(self, lines: list[str]) -> None:
        """Load config from key=value lines."""
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key.endswith("-url"):
                name = key[:-4]
                port = 443
                port_key = f"{name}-port"
                if port_key in self._endpoints:
                    port = self._endpoints[port_key].port
                self.set_endpoint(name, value, port)
            elif key.endswith("-port"):
                name = key[:-5]
                if name in self._endpoints:
                    self._endpoints[name].port = int(value)
                else:
                    self._endpoints[name] = TelemetryEndpoint(name=name, url="", port=int(value))

    def disable(self) -> None:
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    def is_enabled(self) -> bool:
        return self._enabled

    def all_endpoints(self) -> list[str]:
        return list(self._endpoints.keys())

    def missing_endpoints(self, required: list[str]) -> list[str]:
        """Return names of required endpoints that are missing."""
        return [name for name in required if name not in self._endpoints]