# Dashboard design

Why the pages look as they do: what to keep when you change one, and what was tried and dropped.

## A page

- One question a page, general on Home and specific behind it: Grafana's
  [best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/).
- Home answers "is anything wrong?" on one screen. A number that is never wrong goes on the page behind it.
- Link, never copy: a panel lives on one page, and a Home tile opens the page that explains it.
- Server is the box, Services what runs on it: a panel per container is Services'.
- Explore opens empty, so Logs and Traces are pages that show the latest on open; a row there opens
  Explore on that one trace. Digging further is Explore's.

## The panel for the job

| The reader asks | Panel |
|---|---|
| Is it fine? | Stat, filled (`colorMode: background_solid`), in its state's colour |
| How near its limit? | Gauge with `sparkline`, its thresholds the alert rule's |
| How did it move? | Time series in its state's colour (`color.mode: thresholds`, `gradientMode: scheme`, `thresholdsStyle: dashed`) |
| When was it down? | State timeline, a value mapping per state |
| Which of many, now? | Horizontal bar gauge over an instant `topk`, or a table with `gauge` cells |
| Which of many, over time? | Time series in `palette-classic`, a table legend on the right sorted by last value |
| Which run, which alert? | Table; a `regex` value mapping turns an ID into words and keeps its link |

## Shape

- The grid stretches with the screen: lay out at 1920 px, then look at 3840. Tiles 3 or 4 wide and 4 to 7
  tall hold their shape at both; one 6 wide and 3 tall is a strip at 3840.
- A title cuts off past about 13 letters at 3 wide, 15 at 4 wide.
- Top-left is read first: states, then dials, then histories.
- Only a state fills a tile. A plain number stays unfilled or becomes a dial: filled gray reads as a dead block.

## Colour

- Colour says state before anything else, at the alert rule's own threshold, so a tile and its alert agree.
- A tile that would go red for a small reason is split, as Alerts firing is by level.
- Stacks keep their group colours; only a line per container takes the palette.

## Looking at it

The tests check structure, never looks, and a local Grafana cannot reach the stores. With Colin signed in to
Grafana in the browser pane, post each changed page to `/api/dashboards/db` as a copy in a `Preview` folder
(uid and `/d/` links prefixed `preview-`), look with real data at both widths, then delete the folder.

## Tried and dropped

| Tried | Why it went |
|---|---|
| One page, detail in collapsed rows | Nothing led anywhere, and CI had no room |
| Tiles in the text colour, colour only when wrong | Fine and no data looked the same; it read as plain |
| A legend listing 30 containers in two group colours | It buried the chart |
| A stat's range query for a latest value | Its last point lags a run behind: use `instant` |
| Tabs inside one dashboard | They need Grafana's newer file format and share one time range |
| Add-on panels | Core panels cover every job above; the Business plugins are kept up only through 2026 |
| Grafana's Logs and Traces Drilldown | They need Loki and Tempo; these stores are VictoriaLogs and VictoriaTraces |
