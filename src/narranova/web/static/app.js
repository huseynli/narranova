const themeToggle = document.querySelector("[data-theme-toggle]");
const themeLabel = document.querySelector("[data-theme-label]");
const colorScheme = window.matchMedia("(prefers-color-scheme: dark)");

function storedTheme() {
  try {
    const localTheme = localStorage.getItem("narranova-theme");
    if (localTheme === "light" || localTheme === "dark") return localTheme;
  } catch (error) {
    // Fall through to the same-site cookie when storage is unavailable.
  }
  const cookieTheme = document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith("narranova_theme="))
    ?.split("=")[1];
  return cookieTheme === "light" || cookieTheme === "dark" ? cookieTheme : null;
}

function applyTheme(theme, persist = false) {
  document.documentElement.dataset.theme = theme;
  if (persist) {
    try {
      localStorage.setItem("narranova-theme", theme);
    } catch (error) {
      // The theme still applies for this page when storage is unavailable.
    }
    document.cookie = `narranova_theme=${theme}; Path=/; Max-Age=31536000; SameSite=Lax`;
  }
  if (!themeToggle || !themeLabel) return;
  const dark = theme === "dark";
  themeToggle.setAttribute("aria-pressed", String(dark));
  themeToggle.setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} mode`);
  themeToggle.title = `Switch to ${dark ? "light" : "dark"} mode`;
  themeLabel.textContent = dark ? "Light" : "Dark";
}

if (themeToggle) {
  applyTheme(document.documentElement.dataset.theme || "light");
  themeToggle.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  });
  colorScheme.addEventListener("change", (event) => {
    if (!storedTheme()) applyTheme(event.matches ? "dark" : "light");
  });
}

document.querySelectorAll("[data-instruction]").forEach((button) => {
  button.addEventListener("click", () => {
    const field = document.querySelector("#instruction");
    if (!field) return;
    field.value = button.dataset.instruction || "";
    field.focus();
    document.querySelectorAll("[data-instruction]").forEach((item) => {
      item.classList.toggle("selected", item === button);
    });
  });
});

const providerSelect = document.querySelector("[data-provider-select]");
const profileSelect = document.querySelector("[data-profile-select]");

function filterProfiles() {
  if (!providerSelect || !profileSelect) return;
  const providerId = providerSelect.value;
  const providerKind = providerSelect.selectedOptions[0]?.dataset.kind || "";
  let firstVisible = null;
  Array.from(profileSelect.options).forEach((option) => {
    const visible =
      option.dataset.provider === providerId ||
      (!option.dataset.provider && option.dataset.kind === providerKind);
    option.hidden = !visible;
    option.disabled = !visible;
    if (visible && !firstVisible) firstVisible = option;
  });
  if (!profileSelect.selectedOptions[0] || profileSelect.selectedOptions[0].disabled) {
    profileSelect.value = firstVisible ? firstVisible.value : "";
  }
  document.querySelectorAll("[data-default-kind]").forEach((card) => {
    card.hidden = card.dataset.defaultKind !== providerKind;
  });
  document.querySelectorAll("[data-custom-provider]").forEach((card) => {
    card.hidden = card.dataset.customProvider !== providerId;
  });
  document.querySelectorAll("[data-custom-preview]").forEach((preview) => {
    const hasVisiblePair = Array.from(
      preview.querySelectorAll("[data-custom-provider]")
    ).some((card) => !card.hidden);
    preview.hidden = !hasVisiblePair;
  });
}

if (providerSelect && profileSelect) {
  providerSelect.addEventListener("change", filterProfiles);
  filterProfiles();
}

const studioProvider = document.querySelector("[data-studio-provider]");

function updateStudioModules() {
  if (!studioProvider) return;
  const option = studioProvider.selectedOptions[0];
  const instructions = option?.dataset.instructions !== "false";
  const reference = option?.dataset.reference !== "false";
  document.querySelectorAll('[data-studio-module="instructions"]').forEach((module) => {
    module.hidden = !instructions;
    module.querySelectorAll("textarea, input, select").forEach((field) => {
      field.disabled = !instructions;
    });
  });
  document.querySelectorAll('[data-studio-module="reference"]').forEach((module) => {
    module.hidden = !reference;
    module.querySelectorAll("textarea, input, select").forEach((field) => {
      field.disabled = !reference;
    });
  });
}

if (studioProvider) {
  studioProvider.addEventListener("change", updateStudioModules);
  updateStudioModules();
}
