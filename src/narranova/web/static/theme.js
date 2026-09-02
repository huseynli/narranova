(() => {
  let localTheme = null;
  try {
    localTheme = localStorage.getItem("narranova-theme");
  } catch (error) {
    // A same-site cookie remains available when local storage is restricted.
  }

  const cookieTheme = document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith("narranova_theme="))
    ?.split("=")[1];
  const savedTheme = [localTheme, cookieTheme].find(
    (value) => value === "light" || value === "dark"
  );
  document.documentElement.dataset.theme =
    savedTheme ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
})();
