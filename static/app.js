document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-copy-text]").forEach((button) => {
    button.addEventListener("click", async () => {
      const value = button.dataset.copyText || "";
      try {
        await navigator.clipboard.writeText(value);
        const original = button.textContent;
        button.textContent = "COPIED";
        window.setTimeout(() => { button.textContent = original; }, 1400);
      } catch (_error) {
        button.textContent = "COPY FAILED";
        window.setTimeout(() => { button.textContent = "COPY"; }, 1400);
      }
    });
  });
  const body = document.body;
  const analysisResults = document.getElementById("analysis-results");
  if (analysisResults) {
    analysisResults.scrollIntoView({behavior: "smooth", block: "start"});
  }
  const openModal = (id) => {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    body.classList.add("modal-open");
    modal.querySelector("input, select, textarea, button")?.focus();
  };
  const closeModal = (modal) => {
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    body.classList.remove("modal-open");
  };

  document.querySelectorAll("[data-modal-open]").forEach((button) => {
    button.addEventListener("click", () => openModal(button.dataset.modalOpen));
  });
  document.querySelectorAll("[data-modal-close]").forEach((button) => {
    button.addEventListener("click", () => closeModal(button.closest(".modal-backdrop")));
  });
  document.querySelectorAll(".modal-backdrop").forEach((modal) => {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal(modal);
    });
  });

  document.querySelectorAll(".click-row[data-href]").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("a, button, input, select, label")) return;
      window.location.assign(row.dataset.href);
    });
  });
  document.querySelectorAll("[data-dismiss]").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash")?.remove());
  });

  document.querySelectorAll("[data-helpful]").forEach((button) => {
    button.addEventListener("click", () => {
      button.parentElement.querySelectorAll("[data-helpful]").forEach((item) => {
        item.classList.remove("selected");
        item.setAttribute("aria-pressed", "false");
      });
      button.classList.add("selected");
      button.setAttribute("aria-pressed", "true");
      const original = button.textContent.trim();
      button.textContent = original.includes("Yes") ? "✓ Thanks for your feedback" : "✓ Feedback noted";
    });
  });

  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  const sidebar = document.getElementById("sidebar");
  sidebarToggle?.addEventListener("click", () => sidebar?.classList.toggle("is-visible"));
  document.addEventListener("click", (event) => {
    if (sidebar?.classList.contains("is-visible")
      && !event.target.closest("#sidebar")
      && !event.target.closest("[data-sidebar-toggle]")) sidebar.classList.remove("is-visible");
  });

  const notifyToggle = document.querySelector("[data-notify-toggle]");
  const notifyPopover = document.querySelector("[data-notify-popover]");
  notifyToggle?.addEventListener("click", () => {
    if (!notifyPopover) return;
    const isOpen = !notifyPopover.hidden;
    notifyPopover.hidden = isOpen;
    notifyToggle.setAttribute("aria-expanded", String(!isOpen));
  });
  document.addEventListener("click", (event) => {
    if (notifyPopover && !notifyPopover.hidden && !event.target.closest(".notify-shell")) {
      notifyPopover.hidden = true;
      notifyToggle?.setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      document.querySelectorAll(".modal-backdrop.is-open").forEach(closeModal);
      if (notifyPopover) notifyPopover.hidden = true;
      notifyToggle?.setAttribute("aria-expanded", "false");
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      const search = document.querySelector(".top-search input");
      if (!search) return;
      event.preventDefault();
      search.focus();
    }
  });

  document.querySelectorAll("[data-demo-email]").forEach((button) => {
    button.addEventListener("click", () => {
      const email = document.querySelector("input[name='email']");
      const password = document.querySelector("input[name='password']");
      if (email) email.value = button.dataset.demoEmail || "";
      if (password) password.value = button.dataset.demoPassword || "";
      password?.focus();
      document.querySelectorAll("[data-demo-email]").forEach((item) => item.classList.remove("demo-selected"));
      button.classList.add("demo-selected");
    });
  });
  const assistantScenarios = {
    vpn: {
      title: "VPN outage affecting the sales team",
      category: "Network",
      description: "Several sales colleagues cannot connect to the company VPN. Client calls and access to internal tools are blocked. The issue started about 20 minutes ago."
    },
    security: {
      title: "Suspicious sign-in alert on a finance account",
      category: "Access",
      description: "A finance colleague received an unexpected sign-in notification and does not recognise the device or location. They have not approved any MFA prompts."
    },
    hardware: {
      title: "Shared printer queue stopped on floor 3",
      category: "Hardware",
      description: "Print jobs are stuck for several colleagues. The printer is powered on but no new jobs are coming through."
    }
  };
  document.querySelectorAll("[data-assistant-sample]").forEach((button) => {
    button.addEventListener("click", () => {
      const scenario = assistantScenarios[button.dataset.assistantSample];
      if (!scenario) return;
      const form = button.closest("form");
      if (!form) return;
      form.querySelector('[name="title"]').value = scenario.title;
      form.querySelector('[name="category"]').value = scenario.category;
      form.querySelector('[name="description"]').value = scenario.description;
      form.querySelector('[name="title"]').focus();
      document.querySelectorAll("[data-assistant-sample]").forEach((item) => item.classList.remove("sample-selected"));
      button.classList.add("sample-selected");
    });
  });

  document.querySelectorAll(".filter-search").forEach((form) => {
    const input = form.querySelector("input[name='q']");
    input?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") form.requestSubmit();
    });
  });

  // Poll the protected API so control-room activity and operational counters stay current.
  if (body.dataset.liveUpdates === "true") {
    let cursor = Number.parseInt(body.dataset.activityCursor || "0", 10) || 0;
    let busy = false;
    const liveLabel = document.querySelector("[data-live-label]");
    const liveStatus = document.querySelector(".live-status");
    const feed = document.querySelector("[data-live-feed]");
    const timeAgo = (iso) => {
      const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
      if (seconds < 60) return "JUST NOW";
      if (seconds < 3600) return Math.floor(seconds / 60) + "M AGO";
      if (seconds < 86400) return Math.floor(seconds / 3600) + "H AGO";
      return Math.floor(seconds / 86400) + "D AGO";
    };
    const setMetric = (name, value) => {
      document.querySelectorAll('[data-live-metric="' + name + '"]').forEach((node) => {
        node.textContent = String(value);
      });
    };
    const updateBadge = (count) => {
      document.querySelectorAll("[data-notification-count]").forEach((node) => {
        node.textContent = String(count);
        node.hidden = Number(count) < 1;
      });
    };
    const updateNotificationPopover = (items) => {
      if (!notifyPopover) return;
      notifyPopover.querySelectorAll(".notify-preview-form").forEach((form) => form.remove());
      const footer = notifyPopover.querySelector(".notify-popover-foot");
      const csrf = document.querySelector('input[name="csrf_token"]')?.value || "";
      (items || []).forEach((item) => {
        const form = document.createElement("form");
        form.method = "post";
        form.action = "/notifications/" + item.id + "/read";
        form.className = "notify-preview-form";
        const token = document.createElement("input");
        token.type = "hidden";
        token.name = "csrf_token";
        token.value = csrf;
        const next = document.createElement("input");
        next.type = "hidden";
        next.name = "next";
        next.value = item.href || "/notifications";
        const button = document.createElement("button");
        button.type = "submit";
        button.className = "notify-preview" + (item.is_read ? "" : " unread");
        const kind = document.createElement("span");
        kind.className = "notify-kind kind-" + item.kind;
        kind.textContent = item.kind === "escalation" ? "↗" : item.kind === "system" ? "◉" : "◫";
        const copy = document.createElement("span");
        const title = document.createElement("b");
        title.textContent = item.title;
        const message = document.createElement("small");
        message.textContent = item.message;
        const time = document.createElement("em");
        time.textContent = timeAgo(item.created_at);
        copy.append(title, message, time);
        button.append(kind, copy);
        if (!item.is_read) {
          const unread = document.createElement("i");
          button.append(unread);
        }
        form.append(token, next, button);
        notifyPopover.insertBefore(form, footer);
      });
    };
    const updateIncidentRows = (incidents) => {
      (incidents || []).forEach((ticket) => {
        const row = document.querySelector('[data-ticket-id="' + ticket.id + '"]');
        if (!row) return;
        const priority = row.querySelector(".priority");
        if (priority) {
          priority.className = "priority priority-" + ticket.priority.toLowerCase();
          priority.replaceChildren();
          const dot = document.createElement("i");
          priority.append(dot, document.createTextNode(ticket.priority));
        }
        const status = row.querySelector(".status");
        if (status) {
          status.className = "status status-" + ticket.status.toLowerCase().replaceAll(" ", "-");
          status.textContent = ticket.status;
        }
        const assigneeCell = row.querySelector(".person-cell");
        if (assigneeCell && assigneeCell.lastChild?.nodeType === Node.TEXT_NODE) {
          assigneeCell.lastChild.textContent = ticket.assignee;
        }
        const updatedCell = row.querySelector(".muted-cell");
        if (updatedCell) updatedCell.textContent = timeAgo(ticket.updated_at);
      });
    };
    const addActivity = (item) => {
      if (!feed) return;
      feed.querySelector(".empty-copy")?.remove();
      const row = document.createElement("div");
      row.className = "activity-item live-activity-new";
      const icon = document.createElement("span");
      icon.className = "activity-icon " + (
        item.action.toLowerCase().includes("escalat") ? "activity-violet"
        : item.action.toLowerCase().includes("created") ? "activity-green" : "activity-blue"
      );
      icon.textContent = item.action.toLowerCase().includes("escalat") ? "↗"
        : item.action.toLowerCase().includes("created") ? "+" : "↻";
      const copy = document.createElement("div");
      copy.className = "activity-copy";
      const title = document.createElement("b");
      title.textContent = item.action;
      const detail = document.createElement("p");
      detail.textContent = item.detail;
      const meta = document.createElement("small");
      meta.textContent = item.actor + " · " + timeAgo(item.created_at);
      copy.append(title, detail, meta);
      row.append(icon, copy);
      feed.prepend(row);
      while (feed.children.length > 8) feed.lastElementChild?.remove();
    };
    const poll = async () => {
      if (busy || document.visibilityState === "hidden") {
        window.setTimeout(poll, 8000);
        return;
      }
      busy = true;
      try {
        const response = await fetch("/api/updates?after_id=" + cursor, {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          cache: "no-store"
        });
        if (!response.ok) throw new Error("Live update request failed: " + response.status);
        const payload = await response.json();
        cursor = Math.max(cursor, Number(payload.cursor) || cursor);
        body.dataset.activityCursor = String(cursor);
        (payload.activity || []).forEach(addActivity);
        updateIncidentRows(payload.tickets);
        if (payload.metrics) {
          Object.entries(payload.metrics).forEach(([key, value]) => setMetric(key, value));
          setMetric("on_track", Math.max(0, payload.metrics.total - payload.metrics.breached));
        }
        updateBadge(payload.unread || 0);
        updateNotificationPopover(payload.notifications);
        if (liveLabel) liveLabel.textContent = "LIVE SYNC / " + new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
        liveStatus?.classList.remove("is-offline");
      } catch (_error) {
        if (liveLabel) liveLabel.textContent = "RECONNECTING";
        liveStatus?.classList.add("is-offline");
      } finally {
        busy = false;
        window.setTimeout(poll, 8000);
      }
    };
    window.setTimeout(poll, 1800);
  }
});