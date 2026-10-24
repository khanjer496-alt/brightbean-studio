/**
 * Engagement automation rule form: what it offers follows what the chosen account's network allows.
 *
 * Loaded by plugs/home.html and the composer. `plugsRuleForm(capsId, accountId)` is an Alpine `x-data`
 * component. `capsId` is the id of a `<script type="application/json">` holding a list of
 * `{id, triggers, own, cross, reposters, message_max, metered, interval, max_interval, read_cost, per_day,
 * approval}` (one per account the form can pick; see apps.plugs.views._capability_json), `accountId` the
 * account chosen first (or the only one), `interval` and `windowHours` the previous input.
 * An action the account can do itself shows as is; one only another account can do asks who does it.
 * `reads()` and `cost()` estimate what watching one post costs (apps.plugs.models.reads_per_post).
 */
(function (root) {
    function plugsRuleForm(capsId, accountId, interval, windowHours) {
        const node = document.getElementById(capsId);
        const caps = node ? JSON.parse(node.textContent) : [];
        return {
            caps: caps,
            accountId: accountId || (caps[0] && caps[0].id) || "",
            action: "",
            interval: interval ? Number(interval) : "",
            windowHours: windowHours ? Number(windowHours) : 24,
            current() {
                return (
                    this.caps.find((c) => c.id === String(this.accountId)) || {
                        triggers: [], own: [], cross: [], reposters: [], message_max: 300, metered: false, interval: 15,
                        max_interval: 1440, read_cost: null, per_day: 10, approval: false,
                    }
                );
            },
            actions() {
                const cap = this.current();
                const own = new Set(cap.own.map((a) => a.value));
                const list = cap.own.map((a) => ({ value: a.value, label: a.label, crossOnly: false }));
                cap.cross.forEach((a) => {
                    if (!own.has(a.value)) list.push({ value: a.value, label: a.label, crossOnly: true });
                });
                if (!list.some((a) => a.value === this.action)) this.action = list.length ? list[0].value : "";
                return list;
            },
            canOwn() {
                return this.current().own.some((a) => a.value === this.action);
            },
            reads() {
                const floor = this.current().interval || 1;
                const every = Math.max(Number(this.interval) || floor, floor);
                const hours = Math.max(Number(this.windowHours) || 0, 0);
                return 1 + Math.floor((hours * 60) / every);
            },
            cost() {
                const price = this.current().read_cost;
                return price === null || price === undefined ? null : this.reads() * price;
            },
            needsReposter() {
                const cap = this.current();
                return cap.reposters.length > 0 && cap.cross.some((a) => a.value === this.action);
            },
        };
    }
    root.plugsRuleForm = plugsRuleForm;
})(window);
