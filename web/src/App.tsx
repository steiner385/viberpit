import { Dashboard } from './components/Dashboard'

function App() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="p-4 border-b border-border">
        <h1 className="text-2xl font-bold tracking-tight">ccswitch</h1>
      </header>
      <main className="p-4">
        <Dashboard />
      </main>
    </div>
  )
}

export default App
