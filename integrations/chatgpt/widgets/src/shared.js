/* PostDelegate widget runtime: the MCP Apps bridge (JSON-RPC over postMessage) and small pure helpers.
   Every value that came from a tool result is untrusted and is only ever written with textContent. */
var Pd = (function () {
  var pending = {};
  var nextId = 1;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") node.textContent = String(attrs[key]);
      else node.setAttribute(key, String(attrs[key]));
    });
    (children || []).forEach(function (child) {
      if (child) node.appendChild(child);
    });
    return node;
  }

  function parseDate(value) {
    if (!value) return null;
    var d = new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }

  function formatWhen(value) {
    var d = parseDate(value);
    if (!d) return "Not scheduled";
    return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  function dayKey(d) {
    return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
  }

  /* The Monday (local time) of the week containing ``d``. */
  function weekStart(d) {
    var start = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
    return start;
  }

  function postDate(post) {
    return parseDate(post.scheduled_at) || parseDate(post.published_at) || parseDate(post.proposed_publish_at);
  }

  /* Seven days starting on ``start``, each with the posts that fall on it, earliest first. Posts outside the
     week are returned in ``outside`` so the widget can say how many it left out. */
  function groupByWeek(posts, start) {
    var days = [];
    for (var i = 0; i < 7; i++) {
      var day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      days.push({ date: day, key: dayKey(day), posts: [] });
    }
    var byKey = {};
    days.forEach(function (day) { byKey[day.key] = day; });
    var outside = 0;
    (posts || []).forEach(function (post) {
      var when = postDate(post);
      var day = when && byKey[dayKey(when)];
      if (day) day.posts.push({ post: post, when: when });
      else outside += 1;
    });
    days.forEach(function (day) { day.posts.sort(function (a, b) { return a.when - b.when; }); });
    return { days: days, outside: outside };
  }

  /* The week to show: the one with the most posts in it (the earliest on a tie), so a result that is not about
     "this week" still opens on something. */
  function pickWeek(posts) {
    var counts = {};
    var starts = {};
    (posts || []).forEach(function (post) {
      var when = postDate(post);
      if (!when) return;
      var start = weekStart(when);
      var key = dayKey(start);
      counts[key] = (counts[key] || 0) + 1;
      starts[key] = start;
    });
    var best = null;
    Object.keys(counts).forEach(function (key) {
      if (!best || counts[key] > counts[best] || (counts[key] === counts[best] && starts[key] < starts[best])) best = key;
    });
    return best ? starts[best] : weekStart(new Date());
  }

  function tone(status) {
    if (status === "published") return "ok";
    if (status === "failed" || status === "rejected") return "bad";
    if (/pending|changes|held|on_hold|draft/.test(status || "")) return "warn";
    return "";
  }

  function label(status) {
    return String(status || "unknown").replace(/_/g, " ");
  }

  function chip(status) {
    return el("span", { class: "chip", "data-tone": tone(status), text: label(status) });
  }

  function send(message) {
    window.parent.postMessage(Object.assign({ jsonrpc: "2.0" }, message), "*");
  }

  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      send({ id: id, method: method, params: params });
    });
  }

  function followUp(text) {
    if (window.openai && typeof window.openai.sendFollowUpMessage === "function") {
      return window.openai.sendFollowUpMessage({ prompt: text });
    }
    return request("ui/message", { role: "user", content: { type: "text", text: text } });
  }

  function reportSize() {
    send({
      method: "ui/notifications/size-changed",
      params: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
    });
  }

  /* Calls ``render(structuredContent)`` now and on every new tool result. */
  function start(render) {
    function show(data) {
      render(data && typeof data === "object" ? data : {});
      reportSize();
    }
    window.addEventListener("message", function (event) {
      if (event.source !== window.parent) return;
      var msg = event.data;
      if (!msg || msg.jsonrpc !== "2.0") return;
      if (msg.method === "ui/notifications/tool-result") {
        show(msg.params && msg.params.structuredContent);
      } else if (msg.id !== undefined && pending[msg.id]) {
        var entry = pending[msg.id];
        delete pending[msg.id];
        if (msg.error) entry.reject(msg.error);
        else entry.resolve(msg.result);
      }
    });
    window.addEventListener("openai:set_globals", function (event) {
      var globals = event.detail && event.detail.globals;
      if (globals && globals.toolOutput) show(globals.toolOutput);
    });
    if (window.openai && window.openai.toolOutput) show(window.openai.toolOutput);
    else show({});
    request("ui/initialize", {
      protocolVersion: "2025-06-18",
      appInfo: { name: "postdelegate-widget", version: "1.0.0" },
      appCapabilities: {},
    })
      .then(function () { send({ method: "ui/notifications/initialized", params: {} }); })
      .catch(function () {});
    if (typeof ResizeObserver === "function") new ResizeObserver(reportSize).observe(document.body);
  }

  return {
    el: el, parseDate: parseDate, formatWhen: formatWhen, weekStart: weekStart, postDate: postDate,
    groupByWeek: groupByWeek, pickWeek: pickWeek, tone: tone, label: label, chip: chip,
    followUp: followUp, start: start,
  };
})();
if (typeof module !== "undefined") module.exports = Pd;
