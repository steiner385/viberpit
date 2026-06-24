// heic2any ships its own types once installed; this ambient declaration lets
// `tsc -b` resolve the dynamic import even when the package is absent from the
// worktree's symlinked node_modules. It is harmless when the real types exist.
declare module 'heic2any'
