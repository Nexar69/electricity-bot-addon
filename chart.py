from __future__ import annotations

import os
import tempfile
from datetime import datetime
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from history import parse_timestamp, to_float


def _get_timestamp(item: dict[str, Any]) -> datetime | None:
    value = (
        item.get("last_changed")
        or item.get("last_updated")
    )

    if not value:
        return None

    try:
        return parse_timestamp(value)
    except (TypeError, ValueError):
        return None


def create_statistics_chart(
    history: list[dict[str, Any]],
    stats: dict[str, Any],
    off_voltage: float,
    low_voltage_threshold: float,
    history_hours: int,
) -> str:
    """
    Generate a dark PNG voltage chart and return its file path.
    """
    if len(history) < 2:
        raise ValueError(
            "Not enough history points to create a chart."
        )

    times: list[datetime] = []
    voltages: list[float] = []

    for item in history:
        timestamp = _get_timestamp(item)

        if timestamp is None:
            continue

        times.append(timestamp)
        voltages.append(
            to_float(item.get("state"))
        )

    if len(times) < 2:
        raise ValueError(
            "Not enough valid timestamps to create a chart."
        )

    figure, axis = plt.subplots(
        figsize=(12, 7),
        dpi=150,
    )

    figure.patch.set_facecolor("#101418")
    axis.set_facecolor("#171c21")

    # Shade each period according to the voltage state.
    for index in range(len(times) - 1):
        start = times[index]
        end = times[index + 1]
        voltage = voltages[index]

        if voltage < off_voltage:
            background_color = "#512126"

        elif voltage < low_voltage_threshold:
            background_color = "#5a421d"

        else:
            background_color = "#173c2b"

        axis.axvspan(
            start,
            end,
            color=background_color,
            alpha=0.72,
            linewidth=0,
        )

    # Do not draw a line down to 0 V during outages.
    visible_voltages = [
        voltage if voltage >= off_voltage else float("nan")
        for voltage in voltages
    ]

    axis.step(
        times,
        visible_voltages,
        where="post",
        linewidth=2.2,
        color="#69b7ff",
        label="Напруга",
        zorder=4,
    )

    low_times = []
    low_voltages = []

    for timestamp, voltage in zip(
        times,
        voltages,
    ):
        if (
            off_voltage
            <= voltage
            < low_voltage_threshold
        ):
            low_times.append(timestamp)
            low_voltages.append(voltage)

    if low_times:
        axis.scatter(
            low_times,
            low_voltages,
            s=22,
            color="#ffb74d",
            edgecolors="#101418",
            linewidths=0.4,
            zorder=5,
        )

    axis.axhline(
        low_voltage_threshold,
        linewidth=1.1,
        linestyle="--",
        color="#ffb74d",
        alpha=0.9,
    )

    axis.axhline(
        off_voltage,
        linewidth=1.1,
        linestyle="--",
        color="#ff6b6b",
        alpha=0.9,
    )

    on_voltages = [
        voltage
        for voltage in voltages
        if voltage >= off_voltage
    ]

    if on_voltages:
        minimum_axis = min(
            min(on_voltages) - 10,
            off_voltage - 5,
        )

        maximum_axis = max(
            max(on_voltages) + 10,
            low_voltage_threshold + 10,
        )

        minimum_axis = max(
            0,
            minimum_axis,
        )

        axis.set_ylim(
            minimum_axis,
            maximum_axis,
        )

    else:
        axis.set_ylim(
            max(0, off_voltage - 20),
            low_voltage_threshold + 30,
        )

    axis.set_title(
        f"Статистика напруги за останні {history_hours} год",
        fontsize=17,
        fontweight="bold",
        color="#f4f7fa",
        pad=18,
    )

    axis.set_ylabel(
        "Напруга, В",
        fontsize=11,
        color="#d5dce3",
    )

    axis.set_xlabel(
        "Час",
        fontsize=11,
        color="#d5dce3",
        labelpad=10,
    )

    axis.xaxis.set_major_locator(
        mdates.AutoDateLocator(
            minticks=6,
            maxticks=12,
        )
    )

    axis.xaxis.set_major_formatter(
        mdates.DateFormatter("%H:%M")
    )

    axis.grid(
        True,
        linestyle=":",
        linewidth=0.7,
        alpha=0.28,
        color="#aeb8c2",
    )

    axis.tick_params(
        axis="both",
        colors="#c6ced6",
    )

    for spine in axis.spines.values():
        spine.set_color("#56616c")

    legend_items = [
        Patch(
            facecolor="#173c2b",
            label="Електропостачання є",
        ),
        Patch(
            facecolor="#5a421d",
            label="Низька напруга",
        ),
        Patch(
            facecolor="#512126",
            label="Електропостачання відсутнє",
        ),
    ]

    legend = axis.legend(
        handles=legend_items,
        loc="upper left",
        frameon=True,
        fontsize=9,
        ncol=3,
    )

    legend.get_frame().set_facecolor("#20262c")
    legend.get_frame().set_edgecolor("#56616c")

    for text in legend.get_texts():
        text.set_color("#e9eef2")

    summary = (
    f"Доступність: {stats['uptime_percent']:.2f}%\n"
    f"З електропостачанням: {stats['uptime']}\n"
    f"Без електропостачання: {stats['downtime']}\n"
    f"Відключень: {stats['outages']}   "
    f"Подій низької напруги: "
    f"{stats['low_voltage_events']}"
)

    figure.text(
        0.075,
        0.025,
        summary,
        fontsize=10,
        color="#e5ebf0",
        linespacing=1.45,
        verticalalignment="bottom",
        bbox={
            "boxstyle": "round,pad=0.7",
            "facecolor": "#171c21",
            "edgecolor": "#56616c",
            "alpha": 0.98,
        },
    )

    figure.subplots_adjust(
        left=0.075,
        right=0.97,
        top=0.89,
        bottom=0.25,
    )

    temporary_file = tempfile.NamedTemporaryFile(
        prefix="electricity_statistics_",
        suffix=".png",
        delete=False,
    )

    file_path = temporary_file.name
    temporary_file.close()

    figure.savefig(
        file_path,
        format="png",
        facecolor=figure.get_facecolor(),
        bbox_inches="tight",
    )

    plt.close(figure)

    if not os.path.exists(file_path):
        raise RuntimeError(
            "Chart file was not created."
        )

    return file_path
