/* ==========================================================================
   لیستیا — سامانه مدیریت هوشمند خرید و سفارش‌ها
   طراحی و توسعه: س.م.قتالی
   یک فایل مشترک برای همه صفحه‌ها: پیام‌ها، لایسنس، درخواست‌ها و موتور «ثبت سریع»
   ========================================================================== */

(function (window, document) {
  "use strict";

  const App = {};

  /* ---------------------------------------------------------------- text */

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : text;
    return div.innerHTML;
  }

  function escapeAttr(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
  }

  /* --------------------------------------------------------------- toast */

  let toastWrap = null;

  function toast(text, kind, timeout) {
    if (!toastWrap) {
      toastWrap = document.createElement("div");
      toastWrap.className = "toast-wrap";
      document.body.appendChild(toastWrap);
    }

    const el = document.createElement("div");
    el.className = "toast toast-" + (kind || "info");
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.innerHTML =
      '<span class="toast-icon">' + toastIcon(kind) + "</span>" +
      '<span class="toast-text">' + escapeHtml(text) + "</span>";

    toastWrap.appendChild(el);

    const life = timeout || (kind === "error" ? 5200 : 2600);
    const timer = setTimeout(dismiss, life);
    el.addEventListener("click", dismiss);

    function dismiss() {
      clearTimeout(timer);
      if (!el.isConnected) return;
      el.classList.add("toast-out");
      setTimeout(function () { el.remove(); }, 220);
    }

    return dismiss;
  }

  function toastIcon(kind) {
    if (kind === "success") return "✓";
    if (kind === "error") return "!";
    if (kind === "warning") return "⚠";
    return "•";
  }

  /* ------------------------------------------------------------- network */

  /** fetch + JSON in one step: always resolves to { ok, status, data }. */
  function request(url, options) {
    const opts = Object.assign({ method: "GET" }, options || {});
    opts.headers = Object.assign({ "X-Requested-With": "fetch" }, opts.headers || {});

    if (opts.form) {
      opts.method = opts.method === "GET" ? "POST" : opts.method;
      opts.headers["Content-Type"] = "application/x-www-form-urlencoded";
      opts.body = new URLSearchParams(opts.form);
      delete opts.form;
    }

    try {
      const token = window.localStorage.getItem("listia_auth");
      if (token && String(url).indexOf("auth=") < 0) {
        url += (String(url).indexOf("?") >= 0 ? "&" : "?") + "auth=" + encodeURIComponent(token);
      }
    } catch (err) {}

    return fetch(url, opts).then(function (res) {
      return res
        .json()
        .catch(function () { return {}; })
        .then(function (data) {
          return { ok: res.ok && data.success !== false, status: res.status, data: data };
        });
    });
  }

  /**
   * Optimistic helper: the UI has already changed, this only reverts on failure.
   * background(url, options, onFail)
   */
  function background(url, options, onFail) {
    const opts = Object.assign({ method: "POST" }, options || {});
    return request(url, opts)
      .then(function (result) {
        if (!result.ok) onFail(result.data && result.data.message);
      })
      .catch(function () { onFail("خطا در ارتباط با سرور"); });
  }

  /* ------------------------------------------------------------ entrance */

  function animateRow(row) {
    row.classList.add("row-in");
    setTimeout(function () { row.classList.remove("row-in"); }, 400);
  }

  /* ========================================================= live search */

  const GlobalSearch = {
    timer: null,
    controller: null,
    query: "",

    init(root) {
      if (!root) return;
      this.root = root;
      this.form = root.querySelector("#global-search-form");
      this.input = root.querySelector("#global-search-input");
      this.panel = root.querySelector("#global-search-panel");
      this.results = root.querySelector("#global-search-results");
      this.status = root.querySelector("#global-search-status");
      this.spinner = root.querySelector("#global-search-spinner");
      this.allLink = root.querySelector("#global-search-all");

      this.input.addEventListener("input", () => this.schedule(this.input.value));
      this.input.addEventListener("focus", () => {
        if (this.input.value.trim().length >= 2 && this.results.children.length) this.open();
      });

      this.input.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          this.close();
          this.input.blur();
        }

        if (event.key === "ArrowDown" && !this.panel.hidden) {
          const first = this.resultLinks()[0];
          if (first) {
            event.preventDefault();
            first.focus();
          }
        }
      });

      this.results.addEventListener("keydown", (event) => this.moveInResults(event));

      this.form.addEventListener("submit", (event) => {
        const q = this.input.value.trim();
        if (q) return;
        event.preventDefault();
        this.input.focus();
      });

      document.addEventListener("click", (event) => {
        if (!this.root.contains(event.target)) this.close();
      });

      document.addEventListener("keydown", (event) => {
        if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;

        const tag = (event.target.tagName || "").toLowerCase();

        if (
          ["input", "select", "textarea"].includes(tag) ||
          event.target.isContentEditable
        ) {
          return;
        }

        event.preventDefault();
        this.input.focus();
        this.input.select();
      });
    },

    schedule(raw) {
      clearTimeout(this.timer);

      if (this.controller) this.controller.abort();

      const q = raw.trim();
      this.query = q;
      this.setLoading(false);

      if (!q) {
        this.results.innerHTML = "";
        this.close();
        return;
      }

      this.open();
      this.allLink.href = "/search?q=" + encodeURIComponent(q);

      if (q.length < 2) {
        this.results.innerHTML = "";
        this.status.textContent = "برای جستجو حداقل ۲ حرف وارد کن";
        this.allLink.hidden = true;
        return;
      }

      this.status.textContent = "در حال جستجو…";
      this.results.innerHTML = "";
      this.allLink.hidden = true;
      this.setLoading(true);

      this.timer = setTimeout(() => this.run(q), 150);
    },

    run(q) {
      const controller = new AbortController();
      this.controller = controller;

      fetch("/api/search?q=" + encodeURIComponent(q), {
        signal: controller.signal
      })
        .then(function (response) {
          if (!response.ok) throw new Error("search failed");
          return response.json();
        })
        .then((data) => {
          if (this.input.value.trim() !== q) return;
          this.render(data, q);
        })
        .catch((error) => {
          if (error && error.name === "AbortError") return;
          if (this.input.value.trim() !== q) return;

          this.results.innerHTML = "";
          this.status.textContent = "جستجو انجام نشد؛ دوباره تلاش کن";
          this.allLink.hidden = true;
        })
        .finally(() => {
          if (this.input.value.trim() === q) this.setLoading(false);
        });
    },

    render(data, q) {
      const suppliers = (data.suppliers || []).slice(0, 3);
      const products = (data.results || []).slice(0, 7);
      const total = suppliers.length + products.length;

      if (!total) {
        this.results.innerHTML =
          '<div class="global-search-empty">نتیجه‌ای برای «' +
          escapeHtml(q) +
          "» پیدا نشد.</div>";

        this.status.textContent = "بدون نتیجه";
        this.allLink.hidden = true;
        return;
      }

      const supplierHtml = suppliers.map(function (supplier) {
        const count = Number(supplier.active_count || 0);

        return (
          '<a class="global-search-result" role="option" href="/supplier/' +
          supplier.id +
          '">' +
            '<span class="global-result-icon global-result-supplier">' +
              escapeHtml((supplier.name || "ت").slice(0, 1)) +
            "</span>" +
            '<span class="global-result-copy">' +
              "<strong>" +
                highlightText(supplier.name, q) +
              "</strong>" +
              "<small>تأمین‌کننده · " +
                count +
                " سفارش فعال</small>" +
            "</span>" +
            '<svg class="global-result-arrow" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2">' +
              '<path d="m15 18-6-6 6-6"/>' +
            "</svg>" +
          "</a>"
        );
      }).join("");

      const productHtml = products.map(function (product) {
        const state = product.ordered ? "آرشیو" : "فعال";

        return (
          '<a class="global-search-result" role="option" href="/supplier/' +
          product.supplier_id +
          "?highlight=" +
          product.id +
          '">' +
            '<span class="global-result-icon">' +
              '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8">' +
                '<path d="M6 3h12l3 5-9 13L3 8l3-5Z"/>' +
                '<path d="M3 8h18M9 3l3 5 3-5"/>' +
              "</svg>" +
            "</span>" +
            '<span class="global-result-copy">' +
              "<strong>" +
                highlightText(product.product_name, q) +
              "</strong>" +
              "<small>" +
                escapeHtml(product.supplier_name) +
                " · " +
                escapeHtml(product.quantity) +
                " " +
                escapeHtml(product.unit) +
                " · " +
                state +
              "</small>" +
            "</span>" +
            '<svg class="global-result-arrow" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2">' +
              '<path d="m15 18-6-6 6-6"/>' +
            "</svg>" +
          "</a>"
        );
      }).join("");

      this.results.innerHTML = supplierHtml + productHtml;
      this.status.textContent = total + " نتیجه نزدیک";
      this.allLink.hidden = false;
      this.allLink.href = "/search?q=" + encodeURIComponent(q);
    },

    moveInResults(event) {
      const links = this.resultLinks();
      const current = links.indexOf(event.target.closest("a"));

      if (current < 0) return;

      if (event.key === "Escape") {
        event.preventDefault();
        this.input.focus();
        this.close();
        return;
      }

      if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;

      event.preventDefault();

      const direction = event.key === "ArrowDown" ? 1 : -1;
      links[(current + direction + links.length) % links.length].focus();
    },

    resultLinks() {
      return Array.from(
        this.results.querySelectorAll("a.global-search-result")
      );
    },

    setLoading(loading) {
      if (this.spinner) this.spinner.hidden = !loading;
    },

    open() {
      this.panel.hidden = false;
      this.input.setAttribute("aria-expanded", "true");
    },

    close() {
      this.panel.hidden = true;
      this.input.setAttribute("aria-expanded", "false");
    }
  };

  function highlightText(text, query) {
    const source = String(text == null ? "" : text);
    const index = source.toLowerCase().indexOf(String(query).toLowerCase());

    if (index < 0) return escapeHtml(source);

    return (
      escapeHtml(source.slice(0, index)) +
      "<mark>" +
      escapeHtml(source.slice(index, index + query.length)) +
      "</mark>" +
      escapeHtml(source.slice(index + query.length))
    );
  }

  /* ===================================================================== */
  /* Quick add — used on dashboard and /new-purchase                       */
  /* ===================================================================== */

  const QuickAdd = {
    root: null,
    rowCounter: 0,
    pending: 0,
    saves: new Map(),
    dupCache: new Map(),
    dupTimer: null,
    units: [],

    init(root) {
      if (!root) return;

      this.root = root;
      this.units = JSON.parse(root.dataset.units || "[]");

      this.form = root.querySelector("#qa-form");
      this.supplier = root.querySelector("#qa-supplier");
      this.product = root.querySelector("#qa-product");
      this.quantity = root.querySelector("#qa-quantity");
      this.unit = root.querySelector("#qa-unit");
      this.unitChips = root.querySelector("#qa-unit-chips");
      this.description = root.querySelector("#qa-description");
      this.session = root.querySelector("#qa-session");
      this.body = root.querySelector("#qa-body");
      this.count = root.querySelector("#qa-count");
      this.syncBadge = root.querySelector("#qa-sync");
      this.newSupplierBox = root.querySelector("#qa-new-supplier");
      this.newSupplierInput = root.querySelector("#qa-new-supplier-name");

      this.bindKeyboard();
      this.bindSupplier();
      this.bindUnitChips();
      this.bindDuplicates();

      this.form.addEventListener("submit", (event) => {
        event.preventDefault();
        this.submit();
      });

      const finish = root.querySelector("#qa-finish");

      if (finish) {
        finish.addEventListener("click", () => {
          finish.disabled = true;

          this.allSettled().then(function () {
            window.location.href = "/purchases";
          });
        });
      }

      window.addEventListener("beforeunload", (event) => {
        if (this.pending === 0) return;

        event.preventDefault();
        event.returnValue = "";
      });

      window.addEventListener("online", () => this.retryAllFailed());

      this.restoreSticky();
      this.focusStart();
    },

    /* ---------------------------------------------------------- keyboard */

    bindKeyboard() {
      const chain = [
        this.supplier,
        this.product,
        this.quantity,
        this.description
      ];

      chain.forEach((el, index) => {
        if (!el) return;

        el.addEventListener("keydown", (event) => {
          if (event.key === "Escape") {
            this.clearFields(true);
            return;
          }

          if (event.key !== "Enter") return;

          event.preventDefault();

          if (el === this.quantity) {
            const selectedUnit =
              this.unitChips &&
              this.unitChips.querySelector(".unit-chip.selected");

            if (selectedUnit) {
              selectedUnit.focus();
            } else if (this.description) {
              this.description.focus();
            }

            return;
          }

          if (
            el === this.description ||
            index === chain.length - 1
          ) {
            this.submit();
            return;
          }

          const next = chain[index + 1];

          if (!next) {
            this.submit();
            return;
          }

          next.focus();

          if (next.tagName === "INPUT") {
            next.select();
          }
        });
      });

      document.addEventListener("keydown", (event) => {
        if (event.ctrlKey || event.metaKey || event.altKey) return;

        const tag = (event.target.tagName || "").toLowerCase();

        if (
          tag === "input" ||
          tag === "select" ||
          tag === "textarea"
        ) {
          return;
        }

        if (event.key.toLowerCase() === "n") {
          event.preventDefault();
          if (this.product && !this.product.disabled) {
            this.product.focus();
          }
        }
      });
    },

    /* --------------------------------------------------- supplier picker */

    bindSupplier() {
      this.supplier.addEventListener("change", () => {
        if (this.supplier.value === "__locked_supplier__") {
          this.supplier.value = this.lastSupplier || "";
          toast("در نسخه آزمایشی فقط مجاز به ثبت ۱ تأمین‌کننده هستید. برای افزودن تأمین‌کننده لایسنس تهیه فرمایید.", "warning", 4500);
          openLicenseModal();
          return;
        }

        if (this.supplier.value === "__new__") {
          this.openNewSupplier();
          return;
        }

        this.remember("pm.supplier", this.supplier.value);
        this.prefetchDuplicates();
      });

      if (!this.newSupplierBox) return;

      const save = this.root.querySelector("#qa-new-supplier-save");
      const cancel = this.root.querySelector("#qa-new-supplier-cancel");

      save.addEventListener("click", () => this.createSupplier());
      cancel.addEventListener("click", () => this.closeNewSupplier());

      this.newSupplierInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          this.createSupplier();
        }

        if (event.key === "Escape") {
          event.preventDefault();
          this.closeNewSupplier();
        }
      });
    },

    /* ------------------------------------------------------- unit chips */

    bindUnitChips() {
      if (!this.unitChips) return;

      this.unitChips.addEventListener("click", (event) => {
        const chip = event.target.closest(".unit-chip");

        if (!chip || chip.disabled) return;

        this.selectUnit(chip.dataset.value);
      });

      this.unitChips.addEventListener("keydown", (event) => {
        const chip = event.target.closest(".unit-chip");

        if (!chip || chip.disabled) return;

        if (event.key === "Enter") {
          event.preventDefault();

          this.selectUnit(chip.dataset.value);

          if (this.description) {
            this.description.focus();
            this.description.select();
          }

          return;
        }

        if (
          !["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)
        ) {
          return;
        }

        event.preventDefault();

        const chips = Array.from(
          this.unitChips.querySelectorAll(".unit-chip")
        );

        const current = chips.indexOf(chip);
        let next = current;

        if (event.key === "Home") {
          next = 0;
        } else if (event.key === "End") {
          next = chips.length - 1;
        } else if (event.key === "ArrowLeft") {
          next = (current + 1) % chips.length;
        } else {
          next = (current - 1 + chips.length) % chips.length;
        }

        this.selectUnit(chips[next].dataset.value);
        chips[next].focus();
      });
    },

    selectUnit(value) {
      const selected = value || this.units[0] || "";

      if (this.unit) {
        this.unit.value = selected;
      }

      if (!this.unitChips) return;

      this.unitChips.querySelectorAll(".unit-chip").forEach(function (chip) {
        const active = chip.dataset.value === selected;

        chip.classList.toggle("selected", active);
        chip.setAttribute("aria-checked", active ? "true" : "false");
        chip.tabIndex = active ? 0 : -1;
      });
    },

    openNewSupplier() {
      this.newSupplierBox.hidden = false;
      this.newSupplierBox.classList.add("slide-in");
      this.newSupplierInput.value = "";
      this.newSupplierInput.focus();
    },

    closeNewSupplier() {
      this.newSupplierBox.hidden = true;
      this.supplier.value = this.lastSupplier || "";
      this.product.focus();
    },

    createSupplier() {
      const name = this.newSupplierInput.value.trim();

      if (!name) {
        this.newSupplierInput.focus();
        return;
      }

      const option = document.createElement("option");
      option.value = "pending";
      option.textContent = name;

      this.supplier.insertBefore(
        option,
        this.supplier.lastElementChild
      );

      this.supplier.value = "pending";
      this.newSupplierBox.hidden = true;
      this.product.focus();

      request("/suppliers", {
        form: { name: name }
      })
        .then((result) => {
          if (!result.ok) {
            option.remove();
            this.supplier.value = this.lastSupplier || "";

            const msg = (result.data && result.data.message) || "تأمین‌کننده ثبت نشد";
            toast(msg, "error");

            if (result.status === 403 || (result.data && result.data.license_locked)) {
              openLicenseModal();
            }

            return;
          }

          option.value = result.data.id;
          this.supplier.value = String(result.data.id);

          this.remember(
            "pm.supplier",
            this.supplier.value
          );

          toast(
            "تأمین‌کننده «" +
            result.data.name +
            "» اضافه شد",
            "success"
          );

          if (typeof window.DashLive !== "undefined") {
            window.DashLive.addSupplier(result.data.id, result.data.name);
          }
        })
        .catch(() => {
          option.remove();
          this.supplier.value = this.lastSupplier || "";

          toast(
            "خطا در ارتباط با سرور",
            "error"
          );
        });
    },

    /* -------------------------------------------------------- duplicates */

    bindDuplicates() {
      this.product.addEventListener(
        "input",
        () => this.prefetchDuplicates()
      );
    },

    dupKey(supplierId, name) {
      return (
        String(supplierId) +
        "|" +
        name.trim().toLowerCase()
      );
    },

    fetchDuplicates(supplierId, name) {
      const query = new URLSearchParams({
        supplier: supplierId,
        product: name
      });

      return request(
        "/check-duplicate?" + query.toString()
      )
        .then(function (result) {
          return (
            result.data &&
            result.data.matches
          ) || [];
        })
        .catch(function () {
          return [];
        });
    },

    prefetchDuplicates() {
      clearTimeout(this.dupTimer);

      const supplierId = this.supplier.value;
      const name = this.product.value.trim();

      if (
        !supplierId ||
        supplierId === "__new__" ||
        supplierId === "__locked_supplier__" ||
        name.length < 2
      ) {
        return;
      }

      const key = this.dupKey(
        supplierId,
        name
      );

      if (this.dupCache.has(key)) return;

      this.dupTimer = setTimeout(() => {
        this.fetchDuplicates(
          supplierId,
          name
        ).then((matches) => {
          this.dupCache.set(
            key,
            matches
          );
        });
      }, 200);
    },

    duplicateLines(matches) {
      return matches.map(function (m) {
        const where = m.same_supplier
          ? "همین تأمین‌کننده"
          : "«" + m.supplier_name + "»";

        return (
          "• برای " +
          where +
          " با تعداد " +
          m.quantity +
          " " +
          m.unit +
          " ثبت شده"
        );
      });
    },

    /* ------------------------------------------------------------ submit */

    submit() {
      if (this.root && this.root.dataset.canAddProduct === "false") {
        toast("سقف ۵ محصول نسخه آزمایشی پر شده است. برای ثبت کالای بیشتر لایسنس تهیه کنید.", "error", 4000);
        openLicenseModal();
        return;
      }

      const supplierId = this.supplier.value;
      const name = this.product.value.trim();
      const unit = this.unit.value || this.units[0];
      const quantity = this.quantity.value.trim() || "1";
      const description = this.description
        ? this.description.value.trim()
        : "";

      if (
        !supplierId ||
        supplierId === "__new__" ||
        supplierId === "__locked_supplier__"
      ) {
        this.shake(this.supplier);
        toast(
          "اول تأمین‌کننده را انتخاب کن",
          "error"
        );
        return;
      }

      if (supplierId === "pending") {
        toast(
          "تأمین‌کننده تازه هنوز ذخیره نشده — یک لحظه صبر کن",
          "warning"
        );
        return;
      }

      if (!name) {
        this.shake(this.product);
        this.product.focus();
        return;
      }

      const known = this.dupCache.get(
        this.dupKey(
          supplierId,
          name
        )
      );

      if (known && known.length > 0) {
        const ok = window.confirm(
          "«" +
          name +
          "» قبلاً ثبت شده:\n\n" +
          this.duplicateLines(known).join("\n") +
          "\n\nباز هم ثبت شود؟"
        );

        if (!ok) {
          this.product.select();
          return;
        }
      }

      const item = {
        key: "tmp-" + (++this.rowCounter),
        supplier_id: supplierId,
        supplier_name:
          this.supplier.options[
            this.supplier.selectedIndex
          ].textContent.trim(),
        product_name: name,
        quantity: quantity,
        unit: unit,
        description: description
      };

      this.addRow(item);
      this.clearFields();

      this.save(
        item.key,
        {
          supplier: supplierId,
          product: name,
          quantity: quantity,
          unit: unit,
          description: description
        },
        !known
      );
    },

    save(key, payload, checkDuplicates) {
      const row = this.rowByKey(key);

      if (!row) return;

      this.setState(row, "pending");

      let entry = this.saves.get(key);

      if (!entry) {
        entry = {};

        entry.promise = new Promise(function (resolve) {
          entry.resolve = resolve;
        });

        this.saves.set(key, entry);
      }

      entry.payload = payload;

      this.pending += 1;
      this.updateSync();

      const dupPromise = checkDuplicates
        ? this.fetchDuplicates(
            payload.supplier,
            payload.product
          )
        : Promise.resolve([]);

      request("/new-purchase", {
        form: payload
      })
        .then((result) => {
          this.pending -= 1;
          this.updateSync();

          if (!result.ok) {
            const msg = (result.data && result.data.message) || "ثبت نشد";
            this.fail(key, msg);
            if (result.status === 403 || (result.data && result.data.license_locked)) {
              openLicenseModal();
            }
            return;
          }

          const saved = result.data.product;
          const liveRow = this.rowByKey(key);

          if (liveRow) {
            liveRow.dataset.realId = saved.id;
            this.setState(
              liveRow,
              "saved"
            );
          }

          if (typeof window.DashLive !== "undefined") {
            window.DashLive.onProductSaved(saved);
          }

          this.dupCache.delete(
            this.dupKey(
              payload.supplier,
              payload.product
            )
          );

          if (entry.resolve) {
            entry.resolve(
              String(saved.id)
            );
          }

          dupPromise.then((matches) => {
            const others = (matches || []).filter(function (m) {
              return String(m.id) !== String(saved.id);
            });

            if (others.length) {
              this.flagDuplicate(
                key,
                payload.product,
                others
              );
            }
          });
        })
        .catch(() => {
          this.pending -= 1;
          this.updateSync();

          this.fail(
            key,
            "خطا در ارتباط با سرور"
          );
        });
    },

    fail(key, message) {
      const row = this.rowByKey(key);

      if (row) {
        this.setState(
          row,
          "failed"
        );
      }

      toast(
        message,
        "error"
      );
    },

    retry(key) {
      const entry = this.saves.get(key);

      if (
        !entry ||
        !entry.payload
      ) {
        return;
      }

      this.save(
        key,
        entry.payload,
        false
      );
    },

    retryAllFailed() {
      const rows = this.body
        ? this.body.querySelectorAll("tr.row-failed")
        : [];

      rows.forEach((row) => {
        this.retry(row.dataset.key);
      });
    },

    whenSaved(key) {
      const row = this.rowByKey(key);

      if (
        row &&
        row.dataset.realId
      ) {
        return Promise.resolve(
          row.dataset.realId
        );
      }

      const entry = this.saves.get(key);

      if (
        entry &&
        entry.promise
      ) {
        return entry.promise;
      }

      return Promise.reject(
        new Error("unsaved")
      );
    },

    allSettled() {
      const list = [];

      this.saves.forEach(function (entry) {
        if (entry.promise) {
          list.push(
            entry.promise
          );
        }
      });

      return Promise.all(list).catch(function () {});
    },

    /* -------------------------------------------------------- session UI */

    rowByKey(key) {
      return this.body.querySelector(
        'tr[data-key="' +
        key +
        '"]'
      );
    },

    itemFromRow(row) {
      return {
        key: row.dataset.key,
        supplier_id: row.dataset.supplierId || "",
        supplier_name: row.dataset.supplierName || "",
        product_name: row.dataset.productName || "",
        quantity: row.dataset.quantity || "",
        unit: row.dataset.unit || "",
        description: row.dataset.description || ""
      };
    },

    setRowData(row, item) {
      row.dataset.key = item.key;
      row.dataset.supplierId = item.supplier_id || "";
      row.dataset.supplierName = item.supplier_name || "";
      row.dataset.productName = item.product_name || "";
      row.dataset.quantity = item.quantity || "";
      row.dataset.unit = item.unit || "";
      row.dataset.description = item.description || "";
    },

    addRow(item) {
      this.session.hidden = false;

      const row = document.createElement("tr");

      this.setRowData(
        row,
        item
      );

      row.innerHTML = this.rowHtml(
        item,
        "pending"
      );

      this.body.insertBefore(
        row,
        this.body.firstChild
      );

      animateRow(row);
      this.updateCount();

      return row;
    },

    rowHtml(item, state) {
      const key = item.key;

      return (
        '<td class="cell-status">' +
          this.statusHtml(
            key,
            state || "saved"
          ) +
        "</td>" +

        '<td class="cell-product">' +
          '<span class="qa-name">' +
            escapeHtml(item.product_name) +
          "</span>" +
          '<span class="qa-sub">' +
            escapeHtml(item.supplier_name) +
          "</span>" +
        "</td>" +

        '<td class="mono cell-quantity">' +
          escapeHtml(item.quantity) +
        "</td>" +

        '<td class="cell-unit">' +
          '<span class="tag">' +
            escapeHtml(item.unit) +
          "</span>" +
        "</td>" +

        '<td class="cell-description">' +
          escapeHtml(item.description || "") +
        "</td>" +

        '<td class="cell-actions">' +
          '<button type="button" class="icon-btn" title="ویرایش" onclick="App.QuickAdd.startEdit(\'' +
            key +
          "')\">" +
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
              '<path d="M12 20h9"/>' +
              '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>' +
            "</svg>" +
          "</button>" +

          '<button type="button" class="icon-btn danger" title="حذف" onclick="App.QuickAdd.remove(\'' +
            key +
          "')\">" +
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
              '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>' +
            "</svg>" +
          "</button>" +
        "</td>"
      );
    },

    statusHtml(key, state) {
      if (state === "pending") {
        return (
          '<span class="save-spinner" title="در حال ذخیره…"></span>'
        );
      }

      if (state === "failed") {
        return (
          '<button type="button" class="icon-btn danger" title="ذخیره نشد — تلاش دوباره" onclick="App.QuickAdd.retry(\'' +
            key +
          "')\">" +
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
              '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/>' +
              '<path d="M3 3v5h5"/>' +
            "</svg>" +
          "</button>"
        );
      }

      return (
        '<span class="save-ok" title="ذخیره شد">' +
          '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M20 6 9 17l-5-5"/>' +
          "</svg>" +
        "</span>"
      );
    },

    setState(row, state) {
      row.classList.remove(
        "row-pending",
        "row-saved",
        "row-failed"
      );

      row.classList.add(
        "row-" + state
      );

      row.dataset.state = state;

      const cell = row.querySelector(".cell-status");

      if (cell) {
        cell.innerHTML = this.statusHtml(
          row.dataset.key,
          state
        );
      }

      if (state === "saved") {
        row.classList.add("row-just-saved");

        setTimeout(function () {
          row.classList.remove("row-just-saved");
        }, 900);
      }
    },

    repaint(row, item) {
      row.innerHTML = this.rowHtml(
        item,
        row.dataset.state || "saved"
      );
    },

    flagDuplicate(key, name, matches) {
      const row = this.rowByKey(key);

      if (!row) return;

      row.classList.add("row-duplicate");

      const cell = row.querySelector(".cell-product");

      if (
        cell &&
        !cell.querySelector(".dup-badge")
      ) {
        const badge = document.createElement("span");

        badge.className = "dup-badge";
        badge.textContent = "تکراری";
        badge.title = this.duplicateLines(matches).join("\n");

        cell.appendChild(badge);
      }

      toast(
        "«" +
        name +
        "» قبلاً هم ثبت شده بود",
        "warning"
      );
    },

    startEdit(key) {
      const row = this.rowByKey(key);

      if (
        !row ||
        row.classList.contains("editing")
      ) {
        return;
      }

      row.classList.add("editing");

      const item = this.itemFromRow(row);

      row.querySelector(".cell-product").innerHTML =
        '<input class="field field-sm" style="width:150px" value="' +
        escapeAttr(item.product_name) +
        '">';

      row.querySelector(".cell-quantity").innerHTML =
        '<input class="field field-sm mono" style="width:78px" type="number" value="' +
        escapeAttr(item.quantity) +
        '">';

      row.querySelector(".cell-unit").innerHTML =
        '<select class="field field-sm" style="width:96px">' +
          this.units.map(function (u) {
            return (
              '<option value="' +
              u +
              '"' +
              (u === item.unit ? " selected" : "") +
              ">" +
              u +
              "</option>"
            );
          }).join("") +
        "</select>";

      row.querySelector(".cell-description").innerHTML =
        '<input class="field field-sm" style="width:160px" value="' +
        escapeAttr(item.description) +
        '">';

      row.querySelector(".cell-actions").innerHTML =
        '<button type="button" class="icon-btn" title="ذخیره" onclick="App.QuickAdd.saveEdit(\'' +
          key +
        "')\">" +
          '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M20 6 9 17l-5-5"/>' +
          "</svg>" +
        "</button>" +

        '<button type="button" class="icon-btn" title="لغو" onclick="App.QuickAdd.cancelEdit(\'' +
          key +
        "')\">✕</button>";

      row.querySelector(".cell-product input").focus();
    },

    cancelEdit(key) {
      const row = this.rowByKey(key);

      if (!row) return;

      row.classList.remove("editing");

      this.repaint(
        row,
        this.itemFromRow(row)
      );
    },

    saveEdit(key) {
      const row = this.rowByKey(key);

      if (!row) return;

      const name = row
        .querySelector(".cell-product input")
        .value.trim();

      const quantity = row
        .querySelector(".cell-quantity input")
        .value.trim();

      const unit = row
        .querySelector(".cell-unit select")
        .value;

      const description = row
        .querySelector(".cell-description input")
        .value.trim();

      if (
        !name ||
        !quantity ||
        !unit
      ) {
        toast(
          "اطلاعات محصول کامل نیست",
          "error"
        );
        return;
      }

      const prev = this.itemFromRow(row);

      const next = Object.assign(
        {},
        prev,
        {
          product_name: name,
          quantity: quantity,
          unit: unit,
          description: description
        }
      );

      this.setRowData(
        row,
        next
      );

      row.classList.remove("editing");

      this.repaint(
        row,
        next
      );

      const entry = this.saves.get(key);

      if (
        entry &&
        entry.payload
      ) {
        entry.payload = Object.assign(
          {},
          entry.payload,
          {
            product: name,
            quantity: quantity,
            unit: unit,
            description: description
          }
        );
      }

      this.whenSaved(key)
        .then(function (id) {
          return request(
            "/product/" +
            id +
            "/edit",
            {
              form: {
                product: name,
                quantity: quantity,
                unit: unit,
                description: description
              }
            }
          );
        })
        .then((result) => {
          if (result.ok) return;

          this.setRowData(
            row,
            prev
          );

          this.repaint(
            row,
            prev
          );

          toast(
            (result.data && result.data.message) ||
            "ویرایش ذخیره نشد",
            "error"
          );
        })
        .catch(() => {
          this.setRowData(
            row,
            prev
          );

          this.repaint(
            row,
            prev
          );

          toast(
            "ویرایش ذخیره نشد",
            "error"
          );
        });
    },

    remove(key) {
      const row = this.rowByKey(key);

      if (!row) return;

      const failed = row.dataset.state === "failed";
      const supplierId = row.dataset.supplierId;
      const next = row.nextElementSibling;
      const parent = row.parentNode;
      const backup = row.outerHTML;

      row.classList.add("row-out");

      setTimeout(() => {
        row.remove();
        this.updateCount();
      }, 160);

      if (failed) {
        this.saves.delete(key);
        return;
      }

      this.whenSaved(key)
        .then(function (id) {
          return request(
            "/product/" +
            id +
            "/delete",
            {
              method: "POST"
            }
          );
        })
        .then((result) => {
          if (result.ok) {
            this.saves.delete(key);
            if (typeof window.DashLive !== "undefined") {
              window.DashLive.onProductRemoved(id, supplierId);
            }
            return;
          }

          this.restore(
            parent,
            next,
            backup
          );

          toast(
            "حذف انجام نشد",
            "error"
          );
        })
        .catch(() => {
          this.restore(
            parent,
            next,
            backup
          );

          toast(
            "حذف انجام نشد",
            "error"
          );
        });
    },

    restore(parent, next, html) {
      const holder = document.createElement("tbody");

      holder.innerHTML = html;

      const node = holder.firstElementChild;

      node.classList.remove("row-out");

      if (
        next &&
        next.parentNode === parent
      ) {
        parent.insertBefore(
          node,
          next
        );
      } else {
        parent.appendChild(node);
      }

      animateRow(node);
      this.updateCount();
    },

    /* --------------------------------------------------------- utilities */

    clearFields(all) {
      if (this.product && !this.product.disabled) {
        this.product.value = "";
      }
      if (this.quantity && !this.quantity.disabled) {
        this.quantity.value = "";
      }

      if (this.description && !this.description.disabled) {
        this.description.value = "";
      }

      this.selectUnit(this.units[0]);

      if (all) {
        if (this.product) this.product.blur();
      } else {
        if (this.product && !this.product.disabled) this.product.focus();
      }
    },

    updateCount() {
      const rows = this.body.querySelectorAll("tr").length;

      if (this.count) {
        this.count.textContent = rows;
      }

      this.session.hidden = rows === 0;
    },

    updateSync() {
      if (!this.syncBadge) return;

      this.syncBadge.hidden = this.pending === 0;
    },

    shake(el) {
      el.classList.remove("shake");
      void el.offsetWidth;
      el.classList.add("shake");
      el.focus();
    },

    remember(storeKey, value) {
      try {
        window.localStorage.setItem(
          storeKey,
          value
        );
      } catch (err) {}

      if (storeKey === "pm.supplier") {
        this.lastSupplier = value;
      }
    },

    recall(storeKey) {
      try {
        return window.localStorage.getItem(storeKey);
      } catch (err) {
        return null;
      }
    },

    restoreSticky() {
      const supplierId = this.recall("pm.supplier");

      if (
        supplierId &&
        this.supplier.querySelector(
          'option[value="' +
          supplierId +
          '"]'
        )
      ) {
        this.supplier.value = supplierId;
      }

      this.lastSupplier = this.supplier.value;
      this.selectUnit(this.units[0]);

      try {
        window.localStorage.removeItem("pm.unit");
      } catch (err) {}
    },

    focusStart() {
      if (
        this.supplier.value &&
        this.supplier.value !== "__new__" &&
        this.supplier.value !== "__locked_supplier__"
      ) {
        if (this.product && !this.product.disabled) {
          this.product.focus();
        }
      } else {
        this.supplier.focus();
      }
    }
  };

  /* ========================================================= License UI */

  function openLicenseModal() {
    const modal = document.getElementById("license-modal");
    if (!modal) return;
    modal.hidden = false;
    modal.classList.add("modal-open");
    const input = document.getElementById("modal-license-key");
    if (input) {
      setTimeout(() => input.focus(), 100);
    }
  }

  function closeLicenseModal() {
    const modal = document.getElementById("license-modal");
    if (!modal) return;
    modal.classList.remove("modal-open");
    modal.hidden = true;
    const errBox = document.getElementById("modal-license-error");
    if (errBox) errBox.style.display = "none";
    const succBox = document.getElementById("modal-license-success");
    if (succBox) succBox.style.display = "none";
  }

  function copyUserCode() {
    const el = document.getElementById("modal-user-code");
    if (!el) return;
    el.select();
    el.setSelectionRange(0, 99999);
    if (navigator.clipboard) {
      navigator.clipboard.writeText(el.value).catch(function () {});
    } else {
      document.execCommand("copy");
    }
    toast("شناسه فعال‌سازی کپی شد", "success");
  }

  function handleLicenseSubmit(e) {
    e.preventDefault();
    const input = document.getElementById("modal-license-key");
    const btn = document.getElementById("modal-license-btn");
    const errBox = document.getElementById("modal-license-error");
    const succBox = document.getElementById("modal-license-success");

    if (errBox) errBox.style.display = "none";
    if (succBox) succBox.style.display = "none";

    const key = input ? input.value.trim() : "";
    if (!key) return;

    if (btn) btn.disabled = true;

    request("/account/license", { form: { license_key: key } })
      .then(function (result) {
        if (btn) btn.disabled = false;
        if (!result.ok) {
          const msg = (result.data && result.data.message) || "کلید لایسنس نامعتبر است.";
          if (errBox) {
            errBox.textContent = msg;
            errBox.style.display = "block";
          }
          toast(msg, "error");
          return;
        }

        const msg = (result.data && result.data.message) || "لایسنس با موفقیت فعال شد!";
        if (succBox) {
          succBox.textContent = msg;
          succBox.style.display = "block";
        }
        toast(msg, "success");

        setTimeout(function () {
          window.location.reload();
        }, 1200);
      })
      .catch(function () {
        if (btn) btn.disabled = false;
        if (errBox) {
          errBox.textContent = "خطا در ارتباط با سرور";
          errBox.style.display = "block";
        }
        toast("خطا در ارتباط با سرور", "error");
      });
  }

  document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") {
      const modal = document.getElementById("license-modal");
      if (modal && !modal.hidden) {
        closeLicenseModal();
      }
    }
  });

  document.addEventListener("click", function(e) {
    const modal = document.getElementById("license-modal");
    if (modal && !modal.hidden && e.target === modal) {
      closeLicenseModal();
    }
  });

  /* ------------------------------------------------------------- exports */

  App.escapeHtml = escapeHtml;
  App.escapeAttr = escapeAttr;
  App.toast = toast;
  App.request = request;
  App.background = background;
  App.animateRow = animateRow;
  App.GlobalSearch = GlobalSearch;
  App.QuickAdd = QuickAdd;

  const DashLive = {
    bump(el, delta) {
      if (!el) return 0;
      const next = Math.max(0, (parseInt(el.textContent, 10) || 0) + delta);
      el.textContent = next;
      return next;
    },

    setActive(n) {
      const el = document.getElementById("stat-active");
      if (el) el.textContent = n;
      const total = document.getElementById("dash-recent-total");
      if (total) total.textContent = n;
    },

    addSupplier(id, name) {
      if (typeof DASH_SUPPLIERS !== "undefined") {
        DASH_SUPPLIERS.push({ id: id, name: name });
      }
      const grid = document.getElementById("dash-supplier-grid");
      const empty = document.getElementById("dash-supplier-empty");
      if (!grid) return;
      if (grid.querySelector('[data-supplier-id="' + id + '"]')) return;

      const card = document.createElement("a");
      card.href = "/supplier/" + id;
      card.className = "supplier-home-card";
      card.dataset.supplierId = String(id);
      card.innerHTML =
        '<span class="supplier-card-avatar">' + escapeHtml(String(name || "ت").slice(0, 1)) + "</span>" +
        '<span class="supplier-card-copy"><strong>' + escapeHtml(name) + "</strong><small>سفارش فعال</small></span>" +
        '<span class="supplier-card-count" data-count>0</span>' +
        '<svg class="supplier-card-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 18-6-6 6-6"/></svg>';
      grid.appendChild(card);
      grid.style.display = "";
      if (empty) empty.style.display = "none";
      this.bump(document.getElementById("stat-suppliers"), 1);
    },

    bumpSupplier(supplierId, delta) {
      const card = document.querySelector('#dash-supplier-grid [data-supplier-id="' + supplierId + '"] [data-count]');
      if (card) this.bump(card, delta);
    },

    onProductSaved(product) {
      this.bump(document.getElementById("stat-active"), 1);
      const total = document.getElementById("dash-recent-total");
      if (total) this.bump(total, 1);
      this.bumpSupplier(product.supplier_id, 1);
      this.prependRecent(product);
    },

    onProductRemoved(id, supplierId) {
      this.bump(document.getElementById("stat-active"), -1);
      const total = document.getElementById("dash-recent-total");
      if (total) this.bump(total, -1);
      if (supplierId) this.bumpSupplier(supplierId, -1);
      const row = document.querySelector('#dash-recent-body tr[data-id="' + id + '"]');
      if (row) {
        row.remove();
        if (typeof updateDashEmpty === "function") updateDashEmpty();
      }
    },

    onOrdered(id, supplierId) {
      this.bump(document.getElementById("stat-active"), -1);
      this.bump(document.getElementById("stat-archived"), 1);
      const total = document.getElementById("dash-recent-total");
      if (total) this.bump(total, -1);
      if (supplierId) this.bumpSupplier(supplierId, -1);
    },

    prependRecent(product) {
      const body = document.getElementById("dash-recent-body");
      if (!body) return;
      if (body.querySelector('tr[data-id="' + product.id + '"]')) return;
      const row = document.createElement("tr");
      row.dataset.id = product.id;
      row.dataset.supplierId = product.supplier_id;
      row.dataset.supplierName = product.supplier_name || "";
      row.dataset.productName = product.product_name || "";
      row.dataset.quantity = product.quantity || "";
      row.dataset.unit = product.unit || "";
      row.dataset.description = product.description || "";
      row.className = "row-link";
      if (typeof paintDashRow === "function") {
        paintDashRow(row, {
          supplier_id: product.supplier_id,
          supplier_name: product.supplier_name,
          product_name: product.product_name,
          quantity: product.quantity,
          unit: product.unit,
          description: product.description || ""
        });
      }
      body.insertBefore(row, body.firstChild);
      while (body.querySelectorAll("tr").length > 15) {
        body.lastElementChild.remove();
      }
      if (typeof updateDashEmpty === "function") updateDashEmpty();
      if (typeof animateRow === "function") animateRow(row);
    }
  };

  window.DashLive = DashLive;

  window.App = App;
  window.escapeHtml = escapeHtml;
  window.escapeAttr = escapeAttr;
  window.background = background;
  window.openLicenseModal = openLicenseModal;
  window.closeLicenseModal = closeLicenseModal;
  window.copyUserCode = copyUserCode;
  window.handleLicenseSubmit = handleLicenseSubmit;

  window.showError = function (text) {
    toast(text, "error");
  };

  window.showSuccess = function (text) {
    toast(text, "success");
  };

  window.showWarning = function (text) {
    toast(text, "warning");
  };

  document.addEventListener("DOMContentLoaded", function () {
    GlobalSearch.init(
      document.getElementById("global-search")
    );

    QuickAdd.init(
      document.getElementById("quick-add")
    );
  });
})(window, document);
