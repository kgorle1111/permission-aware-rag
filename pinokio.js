module.exports = {
  version: "5.0",
  title: "Permission-Aware RAG",
  description: "RAG retrieval that enforces per-user document ACLs at query time",
  menu: async (kernel, info) => {
    const menu = []
    for (const [script, label] of [
      ["start_underwriter.js", "Underwriter Workbench"],
      ["start.js", "Generic Demo"],
    ]) {
      if (info.running(script)) {
        const local = info.local(script)
        if (local && local.url) {
          menu.push({
            default: true,
            icon: "fa-solid fa-rocket",
            text: `Open ${label}`,
            href: local.url,
          })
        }
        menu.push({
          icon: "fa-solid fa-terminal",
          text: `${label} Terminal`,
          href: script,
        })
      } else {
        menu.push({
          icon: "fa-solid fa-power-off",
          text: `Start ${label}`,
          href: script,
        })
      }
    }
    // stdlib-only app: nothing to install, update, or reset
    if (!menu.some(m => m.default)) menu[0].default = true
    return menu
  }
}
