/**
 * The session rail's card stack (D-6): Changes, Context, Environment, Tasks,
 * in that order. `SessionBody` feeds it and decides where it sits — a right
 * column on a pane of 64rem or more, the Details tab below that.
 */
import type { ComponentProps } from 'react'
import { ChangesCard } from './ChangesCard'
import { ContextCard } from './ContextCard'
import { EnvironmentCard } from './EnvironmentCard'
import { TasksCard } from './TasksCard'

export interface SessionRailProps {
  changes: ComponentProps<typeof ChangesCard>
  context: ComponentProps<typeof ContextCard>
  environment: ComponentProps<typeof EnvironmentCard>
  tasks: ComponentProps<typeof TasksCard>
}

export function SessionRail({ changes, context, environment, tasks }: SessionRailProps) {
  return (
    <div className="flex min-w-0 flex-col gap-2 p-2">
      <ChangesCard {...changes} />
      <ContextCard {...context} />
      <EnvironmentCard {...environment} />
      <TasksCard {...tasks} />
    </div>
  )
}
