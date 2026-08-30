let reload = async () => {}

export function setWorkspaceReloader(callback) {
  reload = callback
}

export function reloadWorkspace() {
  return reload()
}
