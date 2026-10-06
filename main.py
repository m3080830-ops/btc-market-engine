import os
import time
import json
import html
import sqlite3
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


# ============================================================
# CONFIG
# ============================================================

SYMBOL = "BTC_USDT"

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

POLL_SECONDS = 30

DB_FILE = "/tmp/market_history.db"

TRAIL_ACTIVATION_R = 1.0

H1_HISTORY = 200
M15_HISTORY = 2000
M5_HISTORY = 2000

M15_CONFIRM_CANDLES = 16
M5_ENTRY_CANDLES = 8
MIN_SETUP_RR = 1.5

PORT = int(
    os.environ.get(
        "PORT",
        "3000"
    )
)


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ts():
    return int(time.time())


def fmt_ts(ts):

    if not ts:
        return "—"

    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def fmt_num(v, d=2):

    if v is None:
        return "—"

    return f"{float(v):,.{d}f}"


def fmt_r(v):

    if v is None:
        return "—"

    return f"{float(v):+.2f}R"


# ============================================================
# DATABASE
# ============================================================

def db_conn():

    c = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    c.row_factory = sqlite3.Row

    return c


def init_db():

    with db_conn() as c:

        c.execute("""
        CREATE TABLE IF NOT EXISTS setups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_time INTEGER NOT NULL UNIQUE,
            direction TEXT NOT NULL,
            setup_price REAL NOT NULL,
            setup_sl REAL,
            status TEXT NOT NULL DEFAULT 'NIEPOTWIERDZONY',

            confirm_time INTEGER,
            confirm_price REAL,

            entry_time INTEGER,
            entry_price REAL,

            position_sl REAL,

            exit_time INTEGER,
            exit_price REAL,
            exit_reason TEXT,
            exit_r REAL,

            setup_max_r REAL,
            setup_min_r REAL,

            position_max_r REAL,
            position_min_r REAL,

            trail_r REAL,

            planned_target REAL,
            planned_rr REAL,
            decision_reason TEXT,

            last_update INTEGER NOT NULL DEFAULT 0
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        columns = {
            row[1]
            for row in c.execute(
                "PRAGMA table_info(setups)"
            ).fetchall()
        }

        for name, sql_type in (
            ("planned_target", "REAL"),
            ("planned_rr", "REAL"),
            ("decision_reason", "TEXT")
        ):
            if name not in columns:
                c.execute(
                    f"ALTER TABLE setups ADD COLUMN {name} {sql_type}"
                )

        c.commit()


def state_get(key, default=None):

    with db_conn() as c:

        row = c.execute(
            """
            SELECT value
            FROM state
            WHERE key=?
            """,
            (key,)
        ).fetchone()

        if row:
            return row[0]

    return default


def state_set(key, value):

    with db_conn() as c:

        c.execute(
            """
            INSERT OR REPLACE INTO state(
                key,
                value
            )
            VALUES(?,?)
            """,
            (
                key,
                str(value)
            )
        )

        c.commit()


# ============================================================
# MEXC
# ============================================================

def api_get(url):

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                "btc-market-engine/1.0"
        }
    )

    with urllib.request.urlopen(
        req,
        timeout=15
    ) as r:

        return json.loads(
            r.read().decode()
        )


def parse_klines(payload):

    if isinstance(
        payload,
        dict
    ):

        data = payload.get(
            "data",
            payload
        )

    else:

        data = payload

    if not isinstance(
        data,
        dict
    ):

        return []

    times = data.get(
        "time",
        []
    )

    opens = data.get(
        "open",
        []
    )

    closes = data.get(
        "close",
        []
    )

    highs = data.get(
        "high",
        []
    )

    lows = data.get(
        "low",
        []
    )

    vols = data.get(
        "vol",
        [0] * len(times)
    )

    out = []

    count = min(
        len(times),
        len(opens),
        len(closes),
        len(highs),
        len(lows)
    )

    for i in range(count):

        out.append({
            "time": int(times[i]),
            "open": float(opens[i]),
            "close": float(closes[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "vol": (
                float(vols[i])
                if i < len(vols)
                else 0.0
            )
        })

    return sorted(
        out,
        key=lambda x: x["time"]
    )


def get_klines(
    interval,
    total,
    end_ts=None
):

    total = min(
        int(total),
        12000
    )

    result = []

    end = (
        int(end_ts)
        if end_ts is not None
        else now_ts()
    )

    pages = 0

    while (
        len(result) < total
        and pages < 20
    ):

        limit = min(
            2000,
            total - len(result)
        )

        qs = urllib.parse.urlencode({
            "interval": interval,
            "limit": limit,
            "end": end
        })

        url = (
            f"{BASE_URL}/{SYMBOL}?"
            f"{qs}"
        )

        page = parse_klines(
            api_get(url)
        )

        if not page:
            break

        result.extend(page)

        oldest = min(
            x["time"]
            for x in page
        )

        if len(page) < limit:
            break

        end = oldest - 1

        pages += 1

    unique = {
        x["time"]: x
        for x in result
    }

    return sorted(
        unique.values(),
        key=lambda x: x["time"]
    )[-total:]


def closed(rows):

    if len(rows) > 1:
        return rows[:-1]

    return []


# ============================================================
# INDICATORS
# ============================================================

def ema(
    values,
    period
):

    if not values:
        return []

    k = 2.0 / (
        period + 1
    )

    out = [
        values[0]
    ]

    for value in values[1:]:

        out.append(
            value * k
            + out[-1] * (1 - k)
        )

    return out


def atr(
    rows,
    period=14
):

    if not rows:
        return []

    tr = []

    prev = rows[0]["close"]

    for row in rows:

        direction_class = (
            "long"
            if row["direction"] == "LONG"
            else "short"
        )

        result_display = (
            fmt_r(row["exit_r"])
            if row["exit_time"]
            else (
                "OTWARTA"
                if row["entry_time"]
                else (
                    fmt_r(row["planned_rr"])
                    if row["planned_rr"] is not None
                    else "—"
                )
            )
        )

        final_stage = (
            row["exit_reason"]
            if row["exit_time"]
            else (
                "OTWARTA"
                if row["entry_time"]
                else row["status"]
            )
        )

        status_text = row["status"]

        status_class = (
            "yes"
            if row["status"] in (
                "OTWARTA",
                "ZAMKNIETA"
            )
            else "no"
        )

        timeline = (
            "H1 SETUP"
            + (
                " → ENTRY"
                if row["entry_time"]
                else ""
            )
            + (
                " → " + final_stage
                if final_stage
                else ""
            )
        )

        sl_display = (
            row["position_sl"]
            or row["setup_sl"]
        )

        max_display = row["position_max_r"]
        min_display = row["position_min_r"]

        body.append(
            f"""
<div
    class="setup-item collapsed"
    data-setup="{row["setup_time"]}">

<div
    class="setup-summary"
    onclick="toggleSetupRow(this)">

<div class="setup-main">
<span>
{fmt_ts(row["setup_time"])}
</span>

<span class="{direction_class}">
{row["direction"]}
</span>
</div>

<div class="setup-summary-right">

<span class="{status_class}">
{status_text}
</span>

<span class="setup-r">
{result_display}
</span>

<span class="setup-chevron">
›
</span>

</div>

</div>

<div class="setup-details">

<div class="setup-stage">
<b>PRZEBIEG</b>
<span>{html.escape(timeline)}</span>
</div>

<div class="setup-grid">

<div class="card">
<b>WYNIK</b>
<span>{result_display}</span>
</div>

<div class="card">
<b>ETAP</b>
<span>{html.escape(final_stage)}</span>
</div>

<div class="card">
<b>PLAN RR</b>
<span>{fmt_r(row["planned_rr"])}</span>
</div>

<div class="card">
<b>CEL</b>
<span>{fmt_num(row["planned_target"])}</span>
</div>

<div class="card">
<b>SETUP</b>
<span>{fmt_num(row["setup_price"])}</span>
</div>

<div class="card">
<b>SL</b>
<span>{fmt_num(sl_display)}</span>
</div>

<div class="card">
<b>ENTRY</b>
<span>{fmt_num(row["entry_price"])}</span>
</div>

<div class="card">
<b>EXIT</b>
<span>{fmt_num(row["exit_price"])}</span>
</div>

<div class="card">
<b>MAX R</b>
<span>{fmt_r(max_display)}</span>
</div>

<div class="card">
<b>MIN R</b>
<span>{fmt_r(min_display)}</span>
</div>

<div class="card">
<b>TRAIL R</b>
<span>{fmt_r(row["trail_r"])}</span>
</div>

<div class="card">
<b>POWÓD</b>
<span>{html.escape(row["decision_reason"] or row["exit_reason"] or "—")}</span>
</div>

</div>

</div>

</div>
"""
        )

    body.append("""
</div>
</section>
""")

    body.append(
        f"""
<section
    id="chartPanel"
    class="panel">

<div class="toolbar">

<select
    id="setupSelect"
    class="select">

{options}

</select>

<button
    class="tf"
    onclick="loadChart('H1')">
H1
</button>

<button
    class="tf"
    onclick="loadChart('M15')">
M15
</button>

<button
    class="tf"
    onclick="loadChart('M5')">
M5
</button>

</div>

<div class="legend">

Świece z MEXC.
Linie: SETUP, SL, M15 CONF, M5 ENTRY.
Pozycja tabeli, strony i wykresu jest zachowywana
przy odświeżeniu.

</div>

<div
    class="chart-scroll"
    id="chartScroll">

<div
    class="chart-yaxis"
    id="chartYAxis">
</div>

<svg
    id="chart"
    xmlns="http://www.w3.org/2000/svg">
</svg>

</div>

<div
    class="info"
    id="chartInfo">

<div class="empty">
Wybierz setup.
</div>

</div>

</section>
"""
    )

    body.append(
r"""
<script>

const TAB_KEY =
    "btc_active_tab_v6";

const PAGE_Y_KEY =
    "btc_page_y_v5";

const CONF_SCROLL_KEY =
    "btc_confirmed_scroll_v5";

const UNCONF_SCROLL_KEY =
    "btc_unconfirmed_scroll_v5";

const CHART_SETUP_KEY =
    "btc_chart_setup_v5";

const CHART_TF_KEY =
    "btc_chart_tf_v5";

const CHART_SCROLL_KEY =
    "btc_chart_scroll_v5";


function activateTab(name){

    const setups =
        document.getElementById(
            "setups"
        );

    const chartPanel =
        document.getElementById(
            "chartPanel"
        );

    const setupsTab =
        document.getElementById(
            "setupsTab"
        );

    const chartTab =
        document.getElementById(
            "chartTab"
        );

    if(setups){
        setups.classList.remove("active");
    }

    if(chartPanel){
        chartPanel.classList.remove("active");
    }

    if(setupsTab){
        setupsTab.classList.remove("active");
    }

    if(chartTab){
        chartTab.classList.remove("active");
    }

    if(name === "chart"){

        if(chartPanel){
            chartPanel.classList.add("active");
        }

        if(chartTab){
            chartTab.classList.add("active");
        }

        localStorage.setItem(
            TAB_KEY,
            "chart"
        );

        initChart();
        return;
    }

    if(setups){
        setups.classList.add("active");
    }

    if(setupsTab){
        setupsTab.classList.add("active");
    }

    localStorage.setItem(
        TAB_KEY,
        "setups"
    );
}


function toggleSetupRow(summary){

    const item =
        summary.closest(
            ".setup-item"
        );

    if(!item){
        return;
    }

    item.classList.toggle(
        "collapsed"
    );

    const setupTime =
        item.dataset.setup;

    if(
        item.classList.contains(
            "collapsed"
        )
    ){
        localStorage.removeItem(
            "btc_open_setup_v6"
        );
    }else{
        localStorage.setItem(
            "btc_open_setup_v6",
            setupTime
        );
    }
}


function restoreOpenSetup(){

    const saved =
        localStorage.getItem(
            "btc_open_setup_v6"
        );

    if(!saved){
        return;
    }

    const item =
        document.querySelector(
            '.setup-item[data-setup="' +
            saved +
            '"]'
        );

    if(item){
        item.classList.remove(
            "collapsed"
        );
    }
}


function saveScroll(){

    localStorage.setItem(
        PAGE_Y_KEY,
        String(
            window.scrollY || 0
        )
    );
}


function restoreScroll(){

    const savedPage =
        parseInt(
            localStorage.getItem(
                PAGE_Y_KEY
            ) || "0",
            10
        ) || 0;

    window.scrollTo(
        0,
        savedPage
    );
}


function bindTableScroll(){
    return;
}




function formatTime(ts){

    if(!ts){

        return "—";
    }

    return new Date(
        Number(ts) * 1000
    )
    .toISOString()
    .replace(
        "T",
        " "
    )
    .slice(
        0,
        16
    )
    + " UTC";
}


function formatPrice(value){

    if(
        value === null
        || value === undefined
    ){

        return "—";
    }

    return Number(value)
        .toFixed(2);
}


function formatR(value){

    if(
        value === null
        || value === undefined
    ){

        return "—";
    }

    return (
        Number(value) >= 0
        ? "+"
        : ""
    )
    + Number(value).toFixed(2)
    + "R";
}


async function initChart(){

    const select =
        document.getElementById(
            "setupSelect"        );

    if(
        !select
        || !select.options.length
    ){

        return;
    }


    let saved =
        localStorage.getItem(
            CHART_SETUP_KEY
        );


    const exists =
        [
            ...select.options
        ].some(
            option =>
                option.value === saved
        );


    if(
        !saved
        || !exists
    ){

        saved =
            select.options[0].value;
    }


    select.value =
        saved;


    const tf =
        localStorage.getItem(
            CHART_TF_KEY
        )
        || "M15";


    await fetchChart(
        saved,
        tf,
        false
    );
}


async function loadChart(tf){

    const select =
        document.getElementById(
            "setupSelect"
        );

    if(
        !select
        || !select.value
    ){

        return;
    }


    localStorage.setItem(
        CHART_SETUP_KEY,
        select.value
    );


    localStorage.setItem(
        CHART_TF_KEY,
        tf
    );


    await fetchChart(
        select.value,
        tf,
        true
    );
}


async function fetchChart(
    setupTime,
    tf,
    center
){

    try{

        const response =
            await fetch(
                "/api/chart?setup_time="
                + encodeURIComponent(
                    setupTime
                )
                + "&tf="
                + encodeURIComponent(
                    tf
                ),
                {
                    cache:
                        "no-store"
                }
            );


        const data =
            await response.json();


        if(!data.ok){

            throw new Error(
                data.error
                || "Błąd wykresu"
            );
        }


        renderChart(
            data,
            center
        );


    }catch(error){

        const info =
            document.getElementById(
                "chartInfo"
            );

        if(info){

            info.innerHTML =
                "<div class='empty'>"
                + "Błąd wykresu: "
                + String(error)
                    .replace(
                        /[<>]/g,
                        ""
                    )
                + "</div>";
        }
    }
}


function renderChart(
    data,
    center
){

    const svg =
        document.getElementById(
            "chart"
        );

    const scroll =
        document.getElementById(
            "chartScroll"
        );

    const yAxis =
        document.getElementById(
            "chartYAxis"
        );


    const candles =
        data.candles
        || [];


    if(
        !svg
        || !scroll
        || !candles.length
    ){

        return;
    }


    const width =
        Math.max(
            1000,
            candles.length * 9
        );

    const height =
        430;


    const left =
        8;

    const right =
        10;

    const top =
        18;

    const bottom =
        28;


    const innerWidth =
        width
        - left
        - right;


    const innerHeight =
        height
        - top
        - bottom;


    let low =
        Math.min(
            ...candles.map(
                c => c.low
            )
        );

    let high =
        Math.max(
            ...candles.map(
                c => c.high
            )
        );


    [
        data.markers.setup_price,
        data.markers.sl,
        data.markers.confirm_price,
        data.markers.entry_price
    ].forEach(
        value => {

            if(
                value !== null
                && value !== undefined
            ){

                low =
                    Math.min(
                        low,
                        Number(value)
                    );

                high =
                    Math.max(
                        high,
                        Number(value)
                    );
            }
        }
    );


    const padding =
        (
            high - low
        ) * 0.06 || 1;


    low -= padding;
    high += padding;


    const x =
        index =>
            left
            + (
                index
                /
                Math.max(
                    1,
                    candles.length - 1
                )
            )
            * innerWidth;


    const y =
        price =>
            top
            + (
                (high - price)
                /
                (high - low)
            )
            * innerHeight;


    const indexByTime = {};


    candles.forEach(
        (candle, index) => {

            indexByTime[
                candle.time
            ] = index;
        }
    );


    let output = [];


    output.push(
        "<rect "
        + "width='"
        + width
        + "' "
        + "height='"
        + height
        + "' "
        + "fill='#0b0b0b'/>"
    );


    if(yAxis){

        yAxis.innerHTML = "";
    }


    for(
        let grid = 0;
        grid <= 5;
        grid++
    ){

        const yy =
            top
            + grid
            * innerHeight
            / 5;


        const price =
            high
            - (
                (high - low)
                * grid
                / 5
            );


        output.push(
            "<line "
            + "x1='"
            + left
            + "' "
            + "y1='"
            + yy
            + "' "
            + "x2='"
            + (
                width - right
            )
            + "' "
            + "y2='"
            + yy
            + "' "
            + "stroke='#202020'/>"
        );


        if(yAxis){

            const label =
                document.createElement(
                    "div"
                );


            label.className =
                "chart-yaxis-label";


            label.style.top =
                yy + "px";


            label.textContent =
                formatPrice(
                    price
                );


            yAxis.appendChild(
                label
            );
        }
    }


    candles.forEach(
        (
            candle,
            index
        ) => {

            const xx =
                x(index);


            const up =
                candle.close
                >= candle.open;


            const color =
                up
                ? "#7ee787"
                : "#ff7b72";


            const openY =
                y(
                    candle.open
                );


            const closeY =
                y(
                    candle.close
                );


            output.push(
                "<line "
                + "x1='"
                + xx
                + "' "
                + "y1='"
                + y(candle.high)
                + "' "
                + "x2='"
                + xx
                + "' "
                + "y2='"
                + y(candle.low)
                + "' "
                + "stroke='"
                + color
                + "'/>"
            );


            output.push(
                "<rect "
                + "x='"
                + (
                    xx - 2.5
                )
                + "' "
                + "y='"
                + Math.min(
                    openY,
                    closeY
                )
                + "' "
                + "width='5' "
                + "height='"
                + Math.max(
                    1,
                    Math.abs(
                        closeY - openY
                    )
                )
                + "' "
                + "fill='"
                + color
                + "'/>"
            );
        }
    );


    function horizontalLine(
        value,
        color,
        label,        dashed
    ){

        if(
            value === null
            || value === undefined
        ){

            return;
        }


        const yy =
            y(
                Number(value)
            );


        output.push(
            "<line "
            + "x1='"
            + left
            + "' "
            + "y1='"
            + yy
            + "' "
            + "x2='"
            + (
                width - right
            )
            + "' "
            + "y2='"
            + yy
            + "' "
            + "stroke='"
            + color
            + "' "
            + "stroke-width='1.5' "
            + (
                dashed
                ? "stroke-dasharray='6 5' "
                : ""
            )
            + "/>"
        );


        output.push(
            "<text "
            + "x='"
            + (
                width - right - 2
            )
            + "' "
            + "y='"
            + (
                yy - 3
            )
            + "' "
            + "text-anchor='end' "
            + "fill='"
            + color
            + "' "
            + "font-size='11'>"
            + label
            + " "
            + formatPrice(value)
            + "</text>"
        );
    }


    function verticalLine(
        timestamp,
        color,
        label
    ){

        if(
            !timestamp
            || indexByTime[
                timestamp
            ] === undefined
        ){

            return;
        }


        const xx =
            x(
                indexByTime[
                    timestamp
                ]
            );


        output.push(
            "<line "
            + "x1='"
            + xx
            + "' "
            + "y1='"
            + top
            + "' "
            + "x2='"
            + xx
            + "' "
            + "y2='"
            + (
                height - bottom
            )
            + "' "
            + "stroke='"
            + color
            + "' "
            + "stroke-dasharray='3 4'/>"
        );


        output.push(
            "<text "
            + "x='"
            + (
                xx + 3
            )
            + "' "
            + "y='"
            + (
                top + 11
            )
            + "' "
            + "fill='"
            + color
            + "' "
            + "font-size='10'>"
            + label
            + "</text>"
        );
    }


    horizontalLine(
        data.markers.setup_price,
        "#58a6ff",
        "SETUP",
        false
    );


    horizontalLine(
        data.markers.sl,
        "#ff7b72",
        "SL",
        true
    );


    horizontalLine(
        data.markers.confirm_price,
        "#c084fc",
        "M15",
        false
    );


    horizontalLine(
        data.markers.entry_price,
        "#f2cc60",
        "ENTRY",
        false
    );


    verticalLine(
        data.markers.setup_time,
        "#58a6ff",
        "SETUP"
    );


    verticalLine(
        data.markers.confirm_time,
        "#c084fc",
        "M15"
    );


    verticalLine(
        data.markers.entry_time,
        "#f2cc60",
        "ENTRY"
    );


    verticalLine(
        data.markers.exit_time,
        "#8b949e",
        "EXIT"
    );


    horizontalLine(
        data.markers.exit_price,
        "#8b949e",
        "EXIT",
        false
    );

    verticalLine(
        data.markers.exit_time,
        "#8b949e",
        "EXIT"
    );

    horizontalLine(
        data.markers.exit_price,
        "#8b949e",
        "EXIT",
        false
    );


    svg.setAttribute(
        "width",
        width
    );


    svg.setAttribute(
        "height",
        height
    );


    svg.setAttribute(
        "viewBox",
        "0 0 "
        + width
        + " "
        + height
    );


    svg.innerHTML =
        output.join("");


    const metrics =
        data.metrics || {};


    let info = "";


    info += `
<div class="cards">

<div class="card">
<b>SETUP</b>
<span>
${formatTime(
    data.setup.time)}
</span>
</div>

<div class="card">
<b>KIERUNEK</b>
<span class="${
    data.setup.direction
    === "LONG"
    ? "yes"
    : "bad"
}">
${data.setup.direction}
</span>
</div>

<div class="card">
<b>STATUS</b>
<span class="${
    data.setup.status
    === "POTWIERDZONY"
    ? "yes"
    : "bad"
}">
${data.setup.status}
</span>
</div>

<div class="card">
<b>SL</b>
<span>
${formatPrice(
    data.markers.sl
)}
</span>
</div>

</div>
`;


    info += `
<div class="cards">

<div class="card">
<b>MAX R</b>
<span>
${formatR(
    metrics.max_r
)}
</span>
</div>

<div class="card">
<b>MIN R</b>
<span>
${formatR(
    metrics.min_r
)}
</span>
</div>

<div class="card">
<b>TRAIL R</b>
<span>
${formatR(
    metrics.trail_r
)}
</span>
</div>

<div class="card">
<b>EXIT</b>
<span>
${metrics.exit_reason || "—"}
${formatR(
    metrics.exit_r
)}
</span>
</div>

</div>
`;


    info += `
<div class="diag-title">
M15 — obserwacja (NIE BLOKUJE WEJŚCIA)
</div>

<div class="diag-scroll">

<table class="diag">

<thead>

<tr>
<th>#</th>
<th>Czas</th>
<th>Sweep</th>
<th>EMA20</th>
<th>Breakout 5</th>
<th>Świeca</th>
<th>PEŁNE</th>
</tr>

</thead>

<tbody>
`;


    (
        data.m15 || []
    ).forEach(
        row => {

            info += `
<tr>

<td>
${row.n}
</td>

<td>
${formatTime(
    row.time
)}
</td>

<td class="${
    row.sweep
    ? "yes"
    : "bad"
}">
${
    row.sweep
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.ema
    ? "yes"
    : "bad"
}">
${
    row.ema
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.breakout
    ? "yes"
    : "bad"
}">
${
    row.breakout
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.candle
    ? "yes"
    : "bad"
}">
${
    row.candle
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.confirm
    ? "yes"
    : "bad"
}">
${
    row.confirm
    ? "POTW."
    : "—"
}
</td>

</tr>
`;
        }
    );


    info += `
</tbody>
</table>

</div>
`;


    if(
        data.setup.status
        === "POTWIERDZONY"
    ){

        info += `
<div class="diag-title">
M5 — obserwacja po wejściu
</div>

<div class="diag-scroll">

<table class="diag">

<thead>

<tr>
<th>#</th>
<th>Czas</th>
<th>Breakout 3</th>
<th>EMA20</th>
<th>Świeca</th>
<th>ENTRY</th>
</tr>

</thead>

<tbody>
`;


        (
            data.m5 || []
        ).forEach(
            row => {

                info += `
<tr>

<td>
${row.n}
</td>

<td>
${formatTime(
    row.time
)}
</td>

<td class="${
    row.breakout
    ? "yes"
    : "bad"
}">
${
    row.breakout
    ? "✓"
    : "✗"
}
</td>

<td class="${    row.ema
    ? "yes"
    : "bad"
}">
${
    row.ema
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.candle
    ? "yes"
    : "bad"
}">
${
    row.candle
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.entry
    ? "yes"
    : "bad"
}">
${
    row.entry
    ? "ENTRY"
    : "—"
}
</td>

</tr>
`;
            }
        );


        info += `
</tbody>
</table>

</div>
`;

    }else{

        const misses = {
            sweep: 0,
            ema: 0,
            breakout: 0,
            candle: 0
        };


        (
            data.m15 || []
        ).forEach(
            row => {

                if(!row.sweep){

                    misses.sweep++;
                }

                if(!row.ema){

                    misses.ema++;
                }

                if(!row.breakout){

                    misses.breakout++;
                }

                if(!row.candle){

                    misses.candle++;
                }
            }
        );


        info += `
<div class="diag-title">
Dlaczego NIEPOTWIERDZONY?
</div>

<div class="legend">

Jedna z 16 świec musi spełnić
jednocześnie wszystkie 4 warunki.

</div>

<div class="legend">

Braki w oknie:

Sweep ${misses.sweep}/16 ·
EMA ${misses.ema}/16 ·
Breakout ${misses.breakout}/16 ·
Świeca ${misses.candle}/16.

</div>
`;
    }


    const infoElement =
        document.getElementById(
            "chartInfo"
        );


    if(infoElement){

        infoElement.innerHTML =
            info;
    }


    requestAnimationFrame(
        () => {

            requestAnimationFrame(
                () => {

                    const saved =
                        localStorage.getItem(
                            CHART_SCROLL_KEY
                        );


                    const setupIndex =
                        indexByTime[
                            data.markers
                                .setup_time
                        ];


                    if(
                        !center
                        && saved !== null
                    ){

                        scroll.scrollLeft =
                            parseInt(
                                saved,
                                10
                            ) || 0;

                    }else if(
                        center
                        && setupIndex
                        !== undefined
                    ){

                        scroll.scrollLeft =
                            Math.max(
                                0,
                                x(setupIndex)
                                - (
                                    scroll.clientWidth
                                    * 0.30
                                )
                            );
                    }


                    localStorage.setItem(
                        CHART_SCROLL_KEY,
                        String(
                            scroll.scrollLeft
                        )
                    );
                }
            );
        }
    );
}


const setupSelect =
    document.getElementById(
        "setupSelect"
    );


if(setupSelect){

    setupSelect.onchange =
        () => {

            localStorage.setItem(
                CHART_SETUP_KEY,
                setupSelect.value
            );


            const tf =
                localStorage.getItem(
                    CHART_TF_KEY
                )
                || "M15";


            fetchChart(
                setupSelect.value,
                tf,
                true
            );
        };
}


const chartScroll =
    document.getElementById(
        "chartScroll"
    );


if(chartScroll){

    chartScroll.onscroll =
        () => {

            localStorage.setItem(
                CHART_SCROLL_KEY,
                String(
                    chartScroll.scrollLeft                )
            );
        };
}


async function refreshDashboard(){

    saveScroll();


    const savedTab =
        localStorage.getItem(
            TAB_KEY
        )
        || "confirmed";


    const savedPageY =
        window.scrollY || 0;


    const savedSetup =
        localStorage.getItem(
            CHART_SETUP_KEY
        );


    const savedTf =
        localStorage.getItem(
            CHART_TF_KEY
        )
        || "M15";


    try{

        const response =
            await fetch(
                window.location.pathname,
                {
                    cache:
                        "no-store"
                }
            );


        const text =
            await response.text();


        const parser =
            new DOMParser();


        const doc =
            parser.parseFromString(
                text,
                "text/html"
            );


        const newConfirmed =
            doc.getElementById(
                "confirmed"
            );


        const newUnconfirmed =
            doc.getElementById(
                "unconfirmed"
            );


        const oldConfirmed =
            document.getElementById(
                "confirmed"
            );


        const oldUnconfirmed =
            document.getElementById(
                "unconfirmed"
            );


        if(
            newConfirmed
            && oldConfirmed
        ){

            oldConfirmed.replaceWith(
                newConfirmed
            );
        }


        if(
            newUnconfirmed
            && oldUnconfirmed
        ){

            oldUnconfirmed.replaceWith(
                newUnconfirmed
            );
        }


        const newPrice =
            doc.querySelector(
                ".price"
            );


        const oldPrice =
            document.querySelector(
                ".price"
            );


        if(
            newPrice
            && oldPrice
        ){

            oldPrice.textContent =
                newPrice.textContent;
        }


        const newMeta =
            doc.querySelector(
                ".meta"
            );


        const oldMeta =
            document.querySelector(
                ".meta"
            );


        if(
            newMeta
            && oldMeta
        ){

            oldMeta.textContent =
                newMeta.textContent;
        }


        const newError =
            doc.querySelector(
                ".err"
            );


        const oldError =
            document.querySelector(
                ".err"
            );


        if(
            newError
            && oldError
        ){

            oldError.textContent =
                newError.textContent;

        }else if(
            newError
            && !oldError
        ){

            const title =
                document.querySelector(
                    "h1"
                );


            if(title){

                title.insertAdjacentElement(
                    "afterend",
                    newError
                );
            }

        }else if(
            !newError
            && oldError
        ){

            oldError.remove();
        }


        const newSelect =
            doc.getElementById(
                "setupSelect"
            );


        const oldSelect =
            document.getElementById(
                "setupSelect"
            );


        if(
            newSelect
            && oldSelect
        ){

            const currentValue =
                oldSelect.value;


            oldSelect.innerHTML =
                newSelect.innerHTML;


            const stillExists =
                [
                    ...oldSelect.options
                ].some(
                    option =>
                        option.value
                        === currentValue
                );


            if(stillExists){

                oldSelect.value =
                    currentValue;

            }else if(
                savedSetup
            ){

                const savedExists =
                    [
                        ...oldSelect.options
                    ].some(
                        option =>
                            option.value
                            === savedSetup
                    );


                if(savedExists){

                    oldSelect.value =
                        savedSetup;
                }
            }
        }


        bindTableScroll();


        activateTab(            savedTab
        );


        requestAnimationFrame(
            () => {

                window.scrollTo(
                    0,
                    savedPageY
                );


                restoreScroll();
            }
        );


        if(
            savedTab === "chart"
        ){

            const select =
                document.getElementById(
                    "setupSelect"
                );


            if(
                select
                && select.value
            ){

                await fetchChart(
                    select.value,
                    savedTf,
                    false
                );
            }
        }


    }catch(error){

        console.log(
            "Refresh error:",
            error
        );
    }


    setTimeout(
        refreshDashboard,
        10000
    );
}


window.addEventListener(
    "beforeunload",
    saveScroll
);


window.addEventListener(
    "load",
    () => {

        bindTableScroll();

        restoreOpenSetup();

        restoreScroll();


        const savedTab =
            localStorage.getItem(
                TAB_KEY
            )
            || "setups";


        activateTab(
            savedTab === "chart"
            ? "chart"
            : "setups"
        );


        setTimeout(
            () => {

                restoreScroll();

            },
            100
        );


        setTimeout(
            refreshDashboard,
            10000
        );
    }
);

</script>
"""
    )

    body.append(
        "</body></html>"
    )

    return "".join(body)


# ============================================================
# HTTP SERVER
# ============================================================

class Handler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        path, _, query = (
            self.path.partition("?")
        )


        if path == "/api/chart":

            try:

                params = (
                    urllib.parse
                    .parse_qs(
                        query
                    )
                )


                setup_time = int(
                    params.get(
                        "setup_time",
                        [""]
                    )[0]
                )


                tf = params.get(
                    "tf",
                    ["M15"]
                )[0]


                payload = chart_payload(
                    setup_time,
                    tf
                )


                raw = json.dumps(
                    payload,
                    separators=(
                        ",",
                        ":"
                    )
                ).encode(
                    "utf-8"
                )


                self.send_response(
                    200
                )


            except Exception as e:

                raw = json.dumps({
                    "ok": False,
                    "error": repr(e)
                }).encode(
                    "utf-8"
                )


                self.send_response(
                    500
                )


            self.send_header(
                "Content-Type",
                "application/json; "
                "charset=utf-8"
            )


            self.send_header(
                "Cache-Control",
                "no-store"
            )


            self.send_header(
                "Content-Length",
                str(len(raw))
            )


            self.end_headers()


            self.wfile.write(
                raw
            )


            return


        if path == "/health":

            payload = {
                "ok": True,
                "last_update":
                    state_get(
                        "last_live_update"
                    ),
                "error":
                    state_get(
                        "latest_error",
                        ""                    )
            }


            raw = json.dumps(
                payload
            ).encode(
                "utf-8"
            )


            self.send_response(
                200
            )


            self.send_header(
                "Content-Type",
                "application/json; "
                "charset=utf-8"
            )


            self.send_header(
                "Content-Length",
                str(len(raw))
            )


            self.end_headers()


            self.wfile.write(
                raw
            )


            return


        raw = dashboard().encode(
            "utf-8"
        )


        self.send_response(
            200
        )


        self.send_header(
            "Content-Type",
            "text/html; "
            "charset=utf-8"
        )


        self.send_header(
            "Cache-Control",
            "no-store"
        )


        self.send_header(
            "Content-Length",
            str(len(raw))
        )


        self.end_headers()


        self.wfile.write(
            raw
        )


    def log_message(
        self,
        *_
    ):

        return


# ============================================================
# ENGINE LOOP
# ============================================================

def engine_loop():

    init_db()

    rebuild_history()


    while True:

        try:

            live_refresh()

        except Exception as e:

            state_set(
                "latest_error",
                repr(e)
            )


        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    init_db()


    threading.Thread(
        target=engine_loop,
        daemon=True
    ).start()


    HTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        Handler
    ).serve_forever()