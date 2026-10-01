// OMP Usage & Cost: subscription usage and API-equivalent cost across every OMP provider and account.
//
// The bar shows the pool usage of each provider (share of the combined quota
// of all its accounts already spent in the fullest window). The panel lists
// every account with its limit windows, reset countdowns, saved reset credits
// and the API-equivalent cost of what it served.
//
// All data comes from bin/omp_usage.py, which wraps `omp usage --json` and an
// incremental cost index over the OMP session logs.

import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "I18n.js" as I18n

Panel {
  id: root
  moduleName: "iamanro.omp-usage"
  ipcTarget: "iamanro.omp-usage"
  // The shell already routes summon/hide/toggle to bar widgets; one handler
  // per monitor would only collide on a single target.
  manageIpc: false

  readonly property color ink: bar ? bar.foreground : Color.foreground
  readonly property color barInk: bar ? bar.barForeground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(ink, 1.55)
  readonly property color track: Style.selectedFillFor(ink, Color.accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property int refreshSec: Math.max(30, Math.min(3600, Number(setting("refreshIntervalSec", 120)) || 120))
  readonly property string ompCommand: String(setting("ompCommand", "omp") || "omp")
  readonly property real alertFraction: Math.max(0.5, Math.min(1, (Number(setting("alertPercent", 90)) || 90) / 100))
  readonly property bool showCostInBar: flag("showCostInBar", false)
  readonly property bool hideEmails: flag("hideEmails", false)
  readonly property bool demoMode: flag("demoMode", false)
  readonly property string lang: I18n.resolve(setting("language", "auto"), {
    LANGUAGE: Quickshell.env("LANGUAGE"),
    LC_ALL: Quickshell.env("LC_ALL"),
    LC_MESSAGES: Quickshell.env("LC_MESSAGES"),
    LANG: Quickshell.env("LANG")
  })
  readonly property var loc: Qt.locale(lang)

  function t(key, args) { return I18n.tr(root.lang, key, args) }

  readonly property string helper: decodeURIComponent(String(Qt.resolvedUrl("bin/omp_usage.py")).replace(/^file:\/\//, ""))
  // The first snapshot indexes every session log (a few seconds per GB);
  // later ones only read what was appended.
  readonly property int deadlineMs: 120000
  readonly property int maxOutputChars: 4 * 1024 * 1024

  property var snapshot: null
  property string failure: ""
  property bool loaded: false
  property double nowMs: Date.now()

  readonly property var providers: snapshot && Array.isArray(snapshot.providers) ? snapshot.providers : []
  readonly property var accounts: snapshot && Array.isArray(snapshot.accounts) ? snapshot.accounts : []
  // Only providers with a quota meter earn a place in the bar; spend-only
  // providers live in the panel.
  readonly property var barProviders: providers.filter(function(p) { return p.percent !== null && p.percent !== undefined })
  readonly property var errors: {
    var list = snapshot && Array.isArray(snapshot.errors) ? snapshot.errors.slice() : []
    if (failure !== "") list.unshift(failure)
    return list
  }
  readonly property real todayCost: {
    var sum = 0
    for (var i = 0; i < providers.length; i++) sum += Number(providers[i].cost ? providers[i].cost.today : 0) || 0
    return sum
  }
  readonly property bool alarming: {
    for (var i = 0; i < providers.length; i++) if (providerAlarming(providers[i])) return true
    return false
  }

  // A setting written from the command line without --json arrives as a string.
  function flag(name, fallback) {
    var value = setting(name, fallback)
    if (typeof value === "string") {
      var text = value.trim().toLowerCase()
      return !(text === "false" || text === "off" || text === "no" || text === "0")
    }
    return value !== false && value !== 0
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

  function providerAlarming(p) {
    return !!p && Number(p.accounts) > 0 && (Number(p.percent) >= root.alertFraction || Number(p.available) === 0)
  }

  function accountsOf(providerId) {
    return root.accounts.filter(function(a) { return a.provider === providerId })
  }

  function percentText(fraction) {
    var v = Number(fraction)
    return fraction === null || fraction === undefined || !isFinite(v) ? "—" : Math.round(v * 100) + "%"
  }

  function money(value) {
    var v = Number(value)
    if (!isFinite(v)) return "—"
    if (v >= 10000) return "$" + (v / 1000).toLocaleString(root.loc, "f", 1) + "k"
    if (v >= 100) return "$" + Math.round(v).toLocaleString(root.loc, "f", 0)
    return "$" + v.toLocaleString(root.loc, "f", 2)
  }

  function duration(ms) {
    if (!(ms > 0)) return root.t("now")
    var minutes = Math.floor(ms / 60000)
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return days + "d " + (hours % 24) + "h"
    if (hours > 0) return hours + "h " + (minutes % 60) + "m"
    return Math.max(1, minutes) + "m"
  }

  function untilText(at) {
    var ms = Number(at)
    return ms > 0 ? root.t("inTime", [root.duration(ms - root.nowMs)]) : ""
  }

  function clockText(at) {
    var ms = Number(at)
    if (!(ms > 0)) return ""
    var d = new Date(ms)
    var sameDay = d.toDateString() === new Date(root.nowMs).toDateString()
    return sameDay ? root.loc.toString(d, root.loc.timeFormat(Locale.ShortFormat)) : d.toLocaleString(root.loc, Locale.ShortFormat)
  }

  function agoText(at) {
    var ms = Number(at)
    if (!(ms > 0)) return ""
    var diff = root.nowMs - ms
    return diff < 60000 ? root.t("agoSeconds", [Math.max(0, Math.round(diff / 1000))]) : root.t("ago", [root.duration(diff)])
  }

  // Providers name windows freely ("5h", "rolling-5h", "7d", "30d", "weekly",
  // "monthly"); the duration decides when it is known, the id otherwise.
  function windowName(id, durationMs) {
    var hours = Number(durationMs) > 0 ? Math.round(Number(durationMs) / 3600000) : 0
    var text = String(id || "").toLowerCase()
    if (hours === 0) {
      var h = text.match(/(\d+)\s*h/)
      var d = text.match(/(\d+)\s*d/)
      if (h) hours = Number(h[1])
      else if (d) hours = Number(d[1]) * 24
      else if (text.indexOf("week") >= 0) hours = 168
      else if (text.indexOf("day") >= 0 || text.indexOf("daily") >= 0) hours = 24
      else if (text.indexOf("month") >= 0) return root.t("month")
      else return String(id || "")
    }
    if (hours <= 48) return root.t("nHours", [hours])
    if (hours >= 28 * 24 && hours <= 31 * 24) return root.t("month")
    return root.t("nDays", [Math.round(hours / 24)])
  }

  // hideEmails keeps the first letter of each part and the TLD, enough to
  // tell accounts apart on a shared screen: "t•••@p•••.me".
  function mask(part) {
    return part.length > 0 ? part.charAt(0) + "•••" : ""
  }

  function displayEmail(email) {
    var text = String(email || "")
    if (!root.hideEmails) return text
    var at = text.indexOf("@")
    if (at < 0) return root.mask(text)
    var domain = text.slice(at + 1)
    var dot = domain.lastIndexOf(".")
    return root.mask(text.slice(0, at)) + "@" + (dot > 0 ? root.mask(domain.slice(0, dot)) + domain.slice(dot) : root.mask(domain))
  }

  function shortEmail(email) {
    var text = String(email || "")
    var at = text.indexOf("@")
    var local = at > 0 ? text.slice(0, at) : text
    return root.hideEmails ? root.mask(local) : local
  }

  function statusColor(status) {
    if (status === "exhausted" || status === "disabled") return root.urgent
    if (status === "limited") return Qt.tint(root.ink, Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.5))
    return root.ink
  }

  function statusText(a) {
    if (a.status === "disabled") return root.t("stDisabled")
    if (a.status === "exhausted") return root.t("stExhausted")
    if (a.status === "limited") return root.t("stLimited")
    return root.t("stOk")
  }

  // Every icon ships as <icon>.svg for dark surfaces and <icon>-light.svg for
  // light ones (identical for coloured marks).
  function iconFor(p) {
    var icon = p ? String(p.icon || "") : ""
    if (icon === "") return ""
    return Qt.resolvedUrl("assets/" + icon + (luminance(root.ink) < 0.5 ? "-light" : "") + ".svg")
  }

  function monogram(p) {
    var words = String(p && p.name || "?").split(/[\s.-]+/).filter(function(w) { return w !== "" })
    return words.length > 1 ? (words[0].charAt(0) + words[1].charAt(0)).toUpperCase() : String(words[0] || "?").slice(0, 2)
  }

  function luminance(c) {
    function channel(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
    return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b)
  }

  function providerLine(p) {
    if (p.percent === null || p.percent === undefined)
      return p.name + " · " + root.t("todayLine", [root.money(p.cost ? p.cost.today : 0)])
    var text = p.name + " " + root.percentText(p.percent) + " (" + root.windowName(p.window, p.windowDurationMs) + ")"
    text += " · " + root.t("free", [p.available, p.accounts])
    if (p.nextResetAt) text += " · " + root.t("resetIn", [root.untilText(p.nextResetAt)])
    return text
  }

  function barTooltip() {
    if (!root.loaded) return "OMP Usage & Cost · " + root.t("loading")
    var lines = root.providers.map(root.providerLine)
    lines.push(root.t("todayApi", [root.money(root.todayCost)]))
    if (root.errors.length > 0) lines.push("⚠ " + root.errors[0])
    return lines.join("\n")
  }

  function limitTooltip(l) {
    var text = String(l.label || root.windowName(l.window, l.durationMs))
    if (l.resetsAt) text += " · " + root.t("resetIn", [root.untilText(l.resetsAt)]) + " (" + root.clockText(l.resetsAt) + ")"
    else text += " · " + root.t("windowIdle")
    return text
  }

  function accountCostTooltip(a) {
    if (!a.cost) return root.t("noMessages")
    var lines = [
      root.t("currentWindow", [root.money(a.cost.window)]) + (a.windowStart ? root.t("since", [root.clockText(a.windowStart)]) : ""),
      root.t("todayLine", [root.money(a.cost.today)]),
      root.t("weekLine", [root.money(a.cost.week)]),
      root.t("monthLine", [root.money(a.cost.month)]),
      root.t("totalLine", [root.money(a.cost.total)])
    ]
    if (a.resetCredits > 0)
      lines.push(root.t("savedResets", [a.resetCredits])
        + (a.resetCreditsExpireAt ? root.t("expires", [root.clockText(a.resetCreditsExpireAt)]) : ""))
    return lines.join("\n")
  }

  // ------------------------------------------------------------ snapshot

  property string abortReason: ""
  // A refresh requested mid-run runs once the current one exits instead of
  // being dropped. If the command changed (demoMode, ompCommand), the running
  // snapshot is killed and its output ignored, so data from the old mode
  // never replaces the new one.
  property bool refreshQueued: false
  property bool superseded: false
  property string runningKey: ""
  readonly property string commandKey: root.ompCommand + "|" + root.demoMode

  function refresh() {
    if (runner.running) {
      root.refreshQueued = true
      if (root.runningKey !== root.commandKey && !root.superseded) {
        root.superseded = true
        runner.signal(9)
      }
      return
    }
    root.refreshQueued = false
    root.abortReason = ""
    var command = ["/usr/bin/python3", "-I", root.helper, "snapshot", "--omp", root.ompCommand]
    if (root.demoMode) command.push("--demo")
    root.runningKey = root.commandKey
    runner.command = command
    runner.running = true
    deadline.restart()
  }

  function drainQueue() {
    if (root.refreshQueued && !runner.running) root.refresh()
  }

  function abort(reason) {
    if (!runner.running) return
    root.abortReason = reason
    runner.signal(9)
  }

  function apply(text) {
    deadline.stop()
    root.nowMs = Date.now()
    Qt.callLater(root.drainQueue)
    if (root.superseded) {
      root.superseded = false
      return
    }
    if (root.abortReason !== "") {
      root.failure = root.abortReason
      root.abortReason = ""
      return
    }
    var payload = null
    try { payload = JSON.parse(String(text || "")) } catch (e) { payload = null }
    if (!payload || !Array.isArray(payload.providers)) {
      root.failure = root.t("snapshotFailed")
      return
    }
    root.failure = ""
    root.snapshot = payload
    root.loaded = true
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Deferred: this handler runs before bindings that read `settings`
  // (demoMode, ompCommand) re-evaluate, so an immediate refresh would start
  // with the previous values.
  onSettingsChanged: Qt.callLater(root.refresh)
  onOpenedChanged: if (opened) {
    root.nowMs = Date.now()
    if (flick) flick.contentY = 0
    root.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Process {
    id: runner
    onExited: Qt.callLater(root.drainQueue)
    stdout: StdioCollector {
      id: output
      waitForEnd: true
      onDataChanged: if (output.text.length > root.maxOutputChars) root.abort(root.t("tooMuchOutput"))
      onStreamFinished: root.apply(output.text)
    }
  }

  Timer {
    interval: root.refreshSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // Countdowns in the tooltip and panel read nowMs instead of Date.now().
  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  Timer {
    id: deadline
    interval: root.deadlineMs
    onTriggered: root.abort(root.t("tooSlow"))
  }

  // ---------------------------------------------------------------- bar

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    active: root.alarming
    tooltipText: root.barTooltip()
    fixedWidth: root.bar && root.bar.vertical ? -1 : readout.implicitWidth + scaledHorizontalMargin * 2
    fixedHeight: root.bar && root.bar.vertical ? readout.implicitHeight + scaledVerticalPadding * 2 : -1
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else root.toggle()
    }

    Grid {
      id: readout
      anchors.centerIn: parent
      columns: root.bar && root.bar.vertical ? 1 : Math.max(1, root.barProviders.length + (root.showCostInBar ? 1 : 0))
      columnSpacing: Style.space(5)
      rowSpacing: Style.space(3)
      flow: Grid.LeftToRight
      verticalItemAlignment: Grid.AlignVCenter
      horizontalItemAlignment: Grid.AlignHCenter

      Text {
        textFormat: Text.PlainText
        visible: root.barProviders.length === 0
        text: root.loaded ? "OMP —" : "OMP …"
        color: root.barInk
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        renderType: Text.NativeRendering
      }

      Repeater {
        model: root.barProviders

        delegate: Item {
          required property var modelData
          required property int index
          readonly property bool alarm: root.providerAlarming(modelData)
          implicitWidth: pair.implicitWidth + (index > 0 && !(root.bar && root.bar.vertical) ? Style.space(6) : 0)
          implicitHeight: pair.implicitHeight

          Row {
            id: pair
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(4)

            ProviderMark {
              anchors.verticalCenter: parent.verticalCenter
              provider: modelData
              size: Style.font.body
              color: root.barInk
            }

            Text {
              textFormat: Text.PlainText
              anchors.verticalCenter: parent.verticalCenter
              text: root.percentText(modelData.percent)
              color: alarm ? root.urgent : root.barInk
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              renderType: Text.NativeRendering
            }
          }
        }
      }

      Text {
        textFormat: Text.PlainText
        visible: root.showCostInBar && root.loaded
        leftPadding: Style.space(4)
        text: root.money(root.todayCost)
        color: Qt.darker(root.barInk, 1.3)
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        renderType: Text.NativeRendering
      }
    }
  }

  // --------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.refresh()
      onMoveRequested: function(dx, dy) {
        if (dy !== 0)
          flick.contentY = root.clamp(flick.contentY + dy * Style.space(56), 0,
                                      Math.max(0, flick.contentHeight - flick.height))
      }
      onTextKey: function(t) { if (t === "r" || t === "R") root.refresh() }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          // Keep the right-aligned values clear of the scrollbar.
          width: flick.width - (flick.interactive ? Style.space(12) : 0)
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "OMP Usage & Cost"
            meta: runner.running && !root.loaded
              ? root.t("indexing")
              : (root.snapshot
                ? (root.demoMode ? root.t("demo") + " · " : "")
                  + root.t("updated", [root.agoText(root.snapshot.generatedAt)])
                  + (runner.running ? " · " + root.t("refreshing") : "")
                : "")
            foreground: root.ink
            fontFamily: root.fontFamily

            iconComponent: Text {
              textFormat: Text.PlainText
              text: "$"
              color: root.alarming ? root.urgent : root.ink
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              font.bold: true
            }

            trailingControl: Column {
              visible: root.loaded
              Text {
                textFormat: Text.PlainText
                anchors.right: parent.right
                text: root.money(root.todayCost)
                color: root.ink
                font.family: root.fontFamily
                font.pixelSize: Style.font.displayLarge
                font.bold: true
              }
              Text {
                textFormat: Text.PlainText
                anchors.right: parent.right
                text: root.t("todayPrice")
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Repeater {
            model: root.errors

            BorderSurface {
              required property var modelData
              width: column.width
              implicitHeight: errorText.implicitHeight + Style.spacing.xl * 2
              color: Util.alpha(root.urgent, 0.10)
              borderSpec: Border.flat(Util.alpha(root.urgent, 0.35), 1)
              radius: Style.cornerRadius

              Text {
                id: errorText
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(12)
                anchors.rightMargin: Style.space(12)
                text: String(modelData)
                color: root.ink
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
                textFormat: Text.PlainText
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.loaded && root.providers.length === 0
            width: parent.width
            topPadding: Style.space(16)
            text: root.t("noAccounts")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: root.providers

            ProviderSection {
              required property var modelData
              width: column.width
              provider: modelData
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: !!root.snapshot && !!root.snapshot.trackedSince
            width: parent.width
            text: root.t("footnote", [root.snapshot && root.snapshot.trackedSince ? root.clockText(root.snapshot.trackedSince) : ""])
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: root.t("hint")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  // One provider: pool meters, next resets, cost, accounts, models.
  component ProviderSection: Column {
    id: section
    property var provider: null
    readonly property var members: provider ? root.accountsOf(provider.id) : []

    spacing: Style.space(10)

    PanelSeparator { foreground: root.ink }

    Item {
      width: parent.width
      implicitHeight: Math.max(sectionIcon.height, sectionTitle.implicitHeight)

      ProviderMark {
        id: sectionIcon
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        provider: section.provider
        size: Style.font.icon
        color: root.ink
      }

      Text {
        textFormat: Text.PlainText
        id: sectionTitle
        anchors.left: sectionIcon.right
        anchors.leftMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        text: section.provider ? section.provider.name : ""
        color: root.ink
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }

      Text {
        textFormat: Text.PlainText
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: section.provider && section.provider.accounts > 0
          ? root.t("free", [section.provider.available, section.provider.accounts])
          : ""
        color: section.provider && section.provider.available === 0 ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    // Pool usage per window across all accounts of the provider.
    Repeater {
      model: section.provider ? section.provider.windows : []

      Column {
        required property var modelData
        width: section.width
        spacing: Style.space(5)
        readonly property bool alarm: Number(modelData.percent) >= root.alertFraction

        Item {
          width: parent.width
          implicitHeight: poolLabel.implicitHeight

          Text {
            textFormat: Text.PlainText
            id: poolLabel
            text: root.t("pool", [root.windowName(modelData.id, modelData.durationMs)])
            color: root.ink
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            textFormat: Text.PlainText
            anchors.right: parent.right
            text: root.percentText(modelData.percent)
            color: alarm ? root.urgent : root.ink
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }
        }

        Meter {
          width: parent.width
          value: Number(modelData.percent)
          alarming: alarm
        }
      }
    }

    InfoRow {
      width: parent.width
      visible: !!(section.provider && section.provider.nextResetAt)
      label: root.t("nextReset")
      value: section.provider && section.provider.nextResetAt
        ? root.untilText(section.provider.nextResetAt) + " · " + root.shortEmail(section.provider.nextResetAccount)
        : ""
      tooltip: section.provider ? root.clockText(section.provider.nextResetAt) : ""
    }

    InfoRow {
      width: parent.width
      visible: !!(section.provider && section.provider.nextFreeAt)
      label: root.t("nextFree")
      value: section.provider && section.provider.nextFreeAt
        ? root.untilText(section.provider.nextFreeAt) + " · " + root.shortEmail(section.provider.nextFreeAccount)
        : ""
      tooltip: section.provider ? root.clockText(section.provider.nextFreeAt) : ""
    }

    // API-equivalent cost of the provider, all accounts plus unattributed.
    Row {
      id: costRow
      width: parent.width
      spacing: Style.space(6)
      readonly property real cellWidth: (width - spacing * 3) / 4

      CostCell { width: costRow.cellWidth; label: root.t("today"); value: section.provider ? section.provider.cost.today : 0 }
      CostCell { width: costRow.cellWidth; label: root.t("w7d"); value: section.provider ? section.provider.cost.week : 0 }
      CostCell { width: costRow.cellWidth; label: root.t("d30"); value: section.provider ? section.provider.cost.month : 0 }
      CostCell { width: costRow.cellWidth; label: root.t("total"); value: section.provider ? section.provider.cost.total : 0 }
    }

    DayChart {
      width: parent.width
      days: section.provider ? (section.provider.days || []) : []
    }

    PanelSectionHeader {
      visible: section.members.length > 0
      text: root.t("accounts")
      foreground: root.ink
      fontFamily: root.fontFamily
    }

    Repeater {
      model: section.members

      AccountRow {
        required property var modelData
        width: section.width
        account: modelData
      }
    }

    InfoRow {
      width: parent.width
      // Spend-only providers have no accounts, so everything would be "unattributed".
      visible: !!(section.provider && section.provider.accounts > 0
                  && section.provider.unattributed && section.provider.unattributed.total > 0)
      label: root.t("unattributed")
      value: section.provider && section.provider.unattributed
        ? root.t("unattributedValue", [root.money(section.provider.unattributed.week), root.money(section.provider.unattributed.total)])
        : ""
      tooltip: root.t("unattributedTip")
    }

    PanelSectionHeader {
      visible: section.provider && section.provider.models && section.provider.models.length > 0
      text: root.t("models")
      foreground: root.ink
      fontFamily: root.fontFamily
    }

    Repeater {
      model: section.provider ? (section.provider.models || []) : []

      ShareRow {
        required property var modelData
        width: section.width
        label: modelData.model
        value: root.money(modelData.cost)
        share: Number(modelData.cost) / Math.max(0.0001, Number(section.provider.models[0].cost))
      }
    }
  }

  // Account: status dot, name, per-window meters with resets, cost.
  component AccountRow: Rectangle {
    id: accountRow
    property var account: null
    readonly property var primaryLimits: account
      ? account.limits.filter(function(l) { return l.primary })
      : []
    readonly property var tierLimits: account
      ? account.limits.filter(function(l) { return !l.primary && l.percent !== null })
      : []

    implicitHeight: accountColumn.implicitHeight + Style.space(16)
    radius: Style.cornerRadius
    color: Util.alpha(root.ink, 0.05)

    Column {
      id: accountColumn
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.margins: Style.space(8)
      spacing: Style.space(6)

      Item {
        width: parent.width
        implicitHeight: accountName.implicitHeight

        Rectangle {
          id: statusDot
          width: Style.space(7)
          height: width
          radius: width / 2
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          color: accountRow.account ? root.statusColor(accountRow.account.status) : root.ink
          opacity: accountRow.account && accountRow.account.status === "ok" ? 0.5 : 1
        }

        Text {
          id: accountName
          anchors.left: statusDot.right
          anchors.leftMargin: Style.space(8)
          anchors.right: accountCost.left
          anchors.rightMargin: Style.space(8)
          text: accountRow.account
            ? root.displayEmail(accountRow.account.email)
              + (accountRow.account.plan ? " · " + accountRow.account.plan : "")
              + (accountRow.account.resetCredits > 0 ? "  ✦" + accountRow.account.resetCredits : "")
            : ""
          color: root.ink
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          elide: Text.ElideMiddle
          textFormat: Text.PlainText
        }

        Text {
          textFormat: Text.PlainText
          id: accountCost
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: accountRow.account && accountRow.account.cost
            ? root.t("windowCost", [root.money(accountRow.account.cost.window)])
            : root.statusText(accountRow.account || {})
          color: accountRow.account && accountRow.account.status !== "ok"
            ? root.statusColor(accountRow.account.status) : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }

        MouseArea {
          id: accountHover
          anchors.fill: parent
          hoverEnabled: true
          acceptedButtons: Qt.NoButton
        }

        PanelToolTip {
          visible: accountHover.containsMouse
          text: accountRow.account
            ? root.statusText(accountRow.account)
              + (accountRow.account.org && !root.hideEmails ? " · " + accountRow.account.org : "")
              + "\n" + root.accountCostTooltip(accountRow.account)
            : ""
          fontFamily: root.fontFamily
        }
      }

      Repeater {
        model: accountRow.primaryLimits

        LimitRow {
          required property var modelData
          width: accountColumn.width
          limit: modelData
          named: !!accountRow.account.tiered
        }
      }

      Text {
        textFormat: Text.PlainText
        visible: accountRow.tierLimits.length > 0
        width: parent.width
        text: accountRow.tierLimits.map(function(l) {
          return String(l.label) + " " + root.percentText(l.percent)
        }).join(" · ")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }
  }

  // Window label, meter, percentage and reset countdown on one line.
  component LimitRow: Item {
    id: limitRow
    property var limit: null
    property bool named: false
    readonly property bool alarm: !!limit && (limit.status === "exhausted" || Number(limit.percent) >= root.alertFraction)

    implicitHeight: Math.max(limitName.implicitHeight, limitReset.implicitHeight)

    Text {
      textFormat: Text.PlainText
      id: limitName
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      // Per-model buckets share a window, so they are told apart by label.
      width: limitRow.named ? Style.space(104) : Style.space(44)
      text: !limitRow.limit ? ""
        : (limitRow.named && limitRow.limit.label ? limitRow.limit.label : root.windowName(limitRow.limit.window, limitRow.limit.durationMs))
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Meter {
      anchors.left: limitName.right
      anchors.right: limitPercent.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      value: limitRow.limit ? Number(limitRow.limit.percent) : -1
      alarming: limitRow.alarm
    }

    Text {
      textFormat: Text.PlainText
      id: limitPercent
      anchors.right: limitReset.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(38)
      horizontalAlignment: Text.AlignRight
      text: limitRow.limit ? root.percentText(limitRow.limit.percent) : ""
      color: limitRow.alarm ? root.urgent : root.ink
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }

    Text {
      textFormat: Text.PlainText
      id: limitReset
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      // Room for the longest "in 6d 23h" phrasing ("через 6d 23h").
      width: Style.space(88)
      horizontalAlignment: Text.AlignRight
      text: limitRow.limit && limitRow.limit.resetsAt ? root.untilText(limitRow.limit.resetsAt) : "—"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    MouseArea {
      id: limitHover
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
    }

    PanelToolTip {
      visible: limitHover.containsMouse
      text: limitRow.limit ? root.limitTooltip(limitRow.limit) : ""
      fontFamily: root.fontFamily
    }
  }

  component InfoRow: Item {
    id: info
    property string label: ""
    property string value: ""
    property string tooltip: ""

    implicitHeight: Math.max(infoLabel.implicitHeight, infoValue.implicitHeight)

    Text {
      textFormat: Text.PlainText
      id: infoLabel
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: info.label
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      id: infoValue
      anchors.right: parent.right
      anchors.left: infoLabel.right
      anchors.leftMargin: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter
      horizontalAlignment: Text.AlignRight
      text: info.value
      color: root.ink
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideLeft
      textFormat: Text.PlainText
    }

    MouseArea {
      id: infoHover
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
    }

    PanelToolTip {
      visible: infoHover.containsMouse && info.tooltip !== ""
      text: info.tooltip
      fontFamily: root.fontFamily
    }
  }

  component CostCell: Rectangle {
    id: cell
    property string label: ""
    property real value: 0

    implicitHeight: cellColumn.implicitHeight + Style.space(10)
    radius: Style.cornerRadius
    color: Util.alpha(root.ink, 0.05)

    Column {
      id: cellColumn
      anchors.centerIn: parent
      spacing: Style.space(1)

      Text {
        textFormat: Text.PlainText
        anchors.horizontalCenter: parent.horizontalCenter
        text: root.money(cell.value)
        color: root.ink
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }

      Text {
        textFormat: Text.PlainText
        anchors.horizontalCenter: parent.horizontalCenter
        text: cell.label
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }

  // Seven bars, one per local day, today in full foreground.
  component DayChart: Item {
    id: chart
    property var days: []
    readonly property real peak: {
      var p = 0
      for (var i = 0; i < days.length; i++) p = Math.max(p, Number(days[i].cost) || 0)
      return Math.max(0.0001, p)
    }

    visible: days.length > 0
    implicitHeight: Style.space(46)

    Row {
      anchors.fill: parent
      spacing: Style.space(4)

      Repeater {
        model: chart.days

        Item {
          id: dayItem
          required property var modelData
          required property int index
          readonly property bool today: index === chart.days.length - 1
          width: (chart.width - Style.space(4) * (chart.days.length - 1)) / Math.max(1, chart.days.length)
          height: chart.height

          Text {
            textFormat: Text.PlainText
            id: dayLabel
            anchors.bottom: parent.bottom
            anchors.horizontalCenter: parent.horizontalCenter
            // Qt numbers weekdays 1 (Monday) to 7 (Sunday); JS getDay() uses 0 for Sunday.
            text: root.loc.dayName(new Date(String(dayItem.modelData.date) + "T00:00:00").getDay() || 7, Locale.ShortFormat)
            color: dayItem.today ? root.ink : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Rectangle {
            anchors.bottom: dayLabel.top
            anchors.bottomMargin: Style.space(3)
            anchors.horizontalCenter: parent.horizontalCenter
            width: parent.width * 0.7
            height: Math.max(2, (parent.height - dayLabel.height - Style.space(3)) * (Number(dayItem.modelData.cost) || 0) / chart.peak)
            radius: Style.space(2)
            color: dayItem.today ? root.ink : Util.alpha(root.ink, 0.45)
          }

          MouseArea {
            id: dayHover
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.NoButton
          }

          PanelToolTip {
            visible: dayHover.containsMouse
            text: dayItem.modelData.date + " · " + root.money(dayItem.modelData.cost)
            fontFamily: root.fontFamily
          }
        }
      }
    }
  }

  // Label with a share bar filling the row behind it.
  component ShareRow: Item {
    id: share
    property string label: ""
    property string value: ""
    property real share: 0

    implicitHeight: shareLabel.implicitHeight + Style.spacing.md

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: Util.alpha(root.ink, 0.05)
    }

    Rectangle {
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: parent.width * root.clamp(share.share, 0, 1)
      radius: Style.cornerRadius
      color: Util.alpha(root.ink, 0.14)
    }

    Text {
      textFormat: Text.PlainText
      id: shareLabel
      anchors.left: parent.left
      anchors.leftMargin: Style.space(8)
      anchors.right: shareValue.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      text: share.label
      color: root.ink
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Text {
      textFormat: Text.PlainText
      id: shareValue
      anchors.right: parent.right
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      text: share.value
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }
  }

  // Provider logo, or a monogram badge for providers without one.
  component ProviderMark: Item {
    id: mark
    property var provider: null
    property real size: Style.font.body
    property color color: root.ink
    readonly property bool hasImage: markImage.status === Image.Ready

    implicitWidth: size
    implicitHeight: size
    width: size
    height: size

    Image {
      id: markImage
      anchors.fill: parent
      sourceSize.width: mark.size * 2
      sourceSize.height: mark.size * 2
      fillMode: Image.PreserveAspectFit
      source: root.iconFor(mark.provider)
      visible: mark.hasImage
    }

    Rectangle {
      anchors.fill: parent
      visible: !mark.hasImage
      radius: Style.space(3)
      color: "transparent"
      border.width: 1
      border.color: Util.alpha(mark.color, 0.7)

      Text {
        textFormat: Text.PlainText
        anchors.centerIn: parent
        text: root.monogram(mark.provider)
        color: mark.color
        font.family: root.fontFamily
        font.pixelSize: Math.max(7, Math.round(mark.size * 0.5))
        font.bold: true
      }
    }
  }

  component Meter: Item {
    id: meter
    property real value: -1
    property bool alarming: false

    implicitHeight: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))
    height: implicitHeight

    Rectangle {
      anchors.fill: parent
      radius: height / 2
      color: root.track
    }

    Rectangle {
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      height: parent.height
      radius: height / 2
      width: parent.width * root.clamp(isFinite(meter.value) ? meter.value : 0, 0, 1)
      color: meter.alarming ? root.urgent : root.ink

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }
  }
}
