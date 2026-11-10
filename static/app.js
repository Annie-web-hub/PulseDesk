document.addEventListener("DOMContentLoaded", () => {
  const openModal = (id) => {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    modal.querySelector("input, select, textarea, button")?.focus();
  };
  const closeModal = (modal) => {
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
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
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      document.querySelectorAll(".modal-backdrop.is-open").forEach(closeModal);
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      const search = document.querySelector(".top-search input");
      if (search) {
        search.style.display = "block";
        search.focus();
      }
    }
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
      && !event.target.closest("[data-sidebar-toggle]")) {
      sidebar.classList.remove("is-visible");
    }
  });

  document.querySelectorAll(".filter-search").forEach((form) => {
    const input = form.querySelector("input[name='q']");
    input?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") form.requestSubmit();
    });
  });
});