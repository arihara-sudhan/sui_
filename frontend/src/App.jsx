import React, { useEffect, useMemo, useRef, useState } from 'react'

const STEP_TYPE = 'navigate_to_website'
const NAVIGATE_ACTION_STEP_TYPE = 'navigate'
const CLICK_STEP_TYPE = 'click'
const FILL_STEP_TYPE = 'fill'
const DELAY_STEP_TYPE = 'delay'
const KEYPRESS_STEP_TYPE = 'keypress'
const ASSIGN_STEP_TYPE = 'assign'
const SCRAPE_STEP_TYPE = 'scrape'
const PATTERN_MATCHER_STEP_TYPE = 'pattern_matcher'
const SMARTIE_STEP_TYPE = 'smartie'
const MESSAGE_BOX_STEP_TYPE = 'message_box'
const CHECK_ELEMENT_EXISTS_STEP_TYPE = 'check_element_exists'
const CHECK_TEXT_EXISTS_STEP_TYPE = 'check_text_exists'
const IF_STEP_TYPE = 'if'
const FOR_STEP_TYPE = 'for'
const USE_JS_STEP_TYPE = 'use_js'
const WRITER_STEP_TYPE = 'writer'
const CALL_RECIPE_STEP_TYPE = 'call_recipe'
const INVOKE_RECIPE_STEP_TYPE = 'invoke_recipe'
const KEYPRESS_OPTIONS = ['Enter', 'Escape', 'Tab', 'Backspace', 'Delete', 'Space', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', 'Insert', 'Control', 'Shift', 'Alt', 'Meta', 'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'.split('')]
const NAVIGATE_ACTIONS = [{ value: 'back', label: 'Back' }, { value: 'forward', label: 'Forward' }, { value: 'refresh', label: 'Refresh' }]
const API_ROOT = 'http://127.0.0.1:8000/api'

const makeKey = (prefix) => `${prefix}-${crypto.randomUUID()}`
const stepLabel = (step) => (
  step.type === CLICK_STEP_TYPE ? 'Click'
    : step.type === NAVIGATE_ACTION_STEP_TYPE ? 'Navigate'
    : step.type === FILL_STEP_TYPE ? 'Fill'
      : step.type === DELAY_STEP_TYPE ? 'Delay'
        : step.type === KEYPRESS_STEP_TYPE ? 'Keypress'
          : step.type === ASSIGN_STEP_TYPE ? 'Assign'
            : step.type === SCRAPE_STEP_TYPE ? 'Scrape'
            : step.type === PATTERN_MATCHER_STEP_TYPE ? 'Pattern Matcher'
              : step.type === SMARTIE_STEP_TYPE ? 'Smartie'
              : step.type === MESSAGE_BOX_STEP_TYPE ? 'Message box'
              : step.type === CHECK_ELEMENT_EXISTS_STEP_TYPE ? 'Check element exists'
                : step.type === CHECK_TEXT_EXISTS_STEP_TYPE ? 'Check text exists'
                  : step.type === IF_STEP_TYPE ? 'If'
                      : step.type === FOR_STEP_TYPE ? (step.settings?.source === 'table' ? 'For each' : 'For loop')
                      : step.type === USE_JS_STEP_TYPE ? 'Use JS'
                      : step.type === WRITER_STEP_TYPE ? 'Writer'
                      : step.type === CALL_RECIPE_STEP_TYPE ? 'Run recipe'
                      : step.type === INVOKE_RECIPE_STEP_TYPE ? 'Invoke'
                      : 'Navigate to website'
)

const friendlyExecutionLog = (message, execution, steps) => {
  const text = String(message || '')
  const match = text.match(/^Step\s+(started|completed|failed):\s*([^\s(;,]+)/i)
  if (!match) return text

  const [, action, stepId] = match
  const typeMatch = text.match(/^Step\s+(?:started|completed|failed):\s*[^\s(;,]+\s*\(([^)]+)\)/i)
  const step = findStepInTree(steps, stepId)
  const stepType = step?.type || typeMatch?.[1] || (execution?.stepId === stepId ? execution.stepType : '')
  const label = stepType ? stepLabel({ type: stepType }) : 'step'
  return `Step ${action.toLowerCase()}: ${label}`
}

const BRANCH_STEP_TYPES = [CHECK_ELEMENT_EXISTS_STEP_TYPE, CHECK_TEXT_EXISTS_STEP_TYPE, IF_STEP_TYPE, FOR_STEP_TYPE]
const branchNamesForStep = (step) => (
  step.type === IF_STEP_TYPE ? [{ key: 'then', label: 'Exists / true' }, { key: 'else', label: 'Not exists / false' }]
    : step.type === FOR_STEP_TYPE ? [{ key: 'body', label: 'Loop body' }]
    : [{ key: 'exists', label: 'Exists' }, { key: 'not_exists', label: 'Not exists' }]
)

const findStepInTree = (steps, stepId) => {
  for (const step of steps || []) {
    if (step.id === stepId) return step
    for (const branch of Object.values(step.branches || {})) {
      const found = findStepInTree(branch, stepId)
      if (found) return found
    }
  }
  return null
}

const buildStepExecutionStates = (execution) => {
  const states = {}
  for (const entry of execution?.logs || []) {
    const message = typeof entry === 'string' ? entry : entry?.message
    const match = String(message || '').match(/^Step\s+(started|completed|failed):\s*([^\s(;,]+)/i)
    if (!match) continue
    const [, action, stepId] = match
    const errorMatch = action.toLowerCase() === 'failed' ? String(message).match(/\berror=(.*)$/i) : null
    states[stepId] = {
      status: action.toLowerCase() === 'started' ? 'running' : action.toLowerCase(),
      error: errorMatch?.[1]?.trim() || '',
    }
  }

  if (execution?.stepId && ['running', 'completed', 'failed'].includes(execution.status)) {
    const current = states[execution.stepId] || {}
    states[execution.stepId] = {
      ...current,
      status: execution.status,
      error: execution.status === 'failed' ? execution.error || current.error || 'Step failed' : current.error,
    }
  }
  return states
}

const latestExecutionStepId = (execution) => {
  let latestId = execution?.stepId || ''
  for (const entry of execution?.logs || []) {
    const message = typeof entry === 'string' ? entry : entry?.message
    const match = String(message || '').match(/^Step\s+(?:started|completed|failed):\s*([^\s(;,]+)/i)
    if (match) latestId = match[1]
  }
  return latestId
}

const executionStepDetails = (step) => {
  if (!step) return ''
  const settings = step.settings || {}
  const shorten = (value, limit = 110) => {
    const text = String(value || '').trim()
    return text.length > limit ? `${text.slice(0, limit)}…` : text
  }
  if (step.type === STEP_TYPE) return shorten(settings.url)
  if (step.type === NAVIGATE_ACTION_STEP_TYPE) return settings.action ? String(settings.action).replace(/^./, (letter) => letter.toUpperCase()) : ''
  if ([CLICK_STEP_TYPE, CHECK_ELEMENT_EXISTS_STEP_TYPE].includes(step.type)) return settings.selector ? `Selector: ${shorten(settings.selector)}` : ''
  if (step.type === CHECK_TEXT_EXISTS_STEP_TYPE) return settings.text ? `Text: “${shorten(settings.text, 90)}”` : ''
  if (step.type === FILL_STEP_TYPE) return settings.selector ? `Selector: ${shorten(settings.selector)}` : ''
  if (step.type === SCRAPE_STEP_TYPE) return settings.selector ? `Selector: ${shorten(settings.selector)}` : ''
  if (step.type === DELAY_STEP_TYPE) return settings.until_load_completes ? 'Until page load completes' : settings.seconds ? `${settings.seconds} seconds` : ''
  if (step.type === KEYPRESS_STEP_TYPE) return settings.key ? `Key: ${settings.key}` : ''
  if (step.type === ASSIGN_STEP_TYPE) return settings.variable ? `${settings.variable} = ${shorten(settings.value)}` : ''
  if (step.type === MESSAGE_BOX_STEP_TYPE) return settings.message ? `Message: ${shorten(settings.message, 90)}` : ''
  if (step.type === WRITER_STEP_TYPE) return settings.table_name ? `Table: ${settings.table_name}` : ''
  if (step.type === USE_JS_STEP_TYPE) return 'JavaScript action'
  if (step.type === IF_STEP_TYPE) return [settings.left, settings.operator, settings.right].filter(Boolean).join(' ')
  if (step.type === FOR_STEP_TYPE) return settings.source === 'table' ? `Table: ${settings.table_name || 'rows'}` : `Range: ${shorten(settings.range_value)}`
  if ([CALL_RECIPE_STEP_TYPE, INVOKE_RECIPE_STEP_TYPE].includes(step.type)) return settings.recipe_name || settings.recipe_id || ''
  return ''
}

const flattenSteps = (steps) => (steps || []).flatMap((step) => [step, ...Object.values(step.branches || {}).flatMap((branch) => flattenSteps(branch))])

const mapStepTree = (steps, stepId, updater) => (steps || []).map((step) => {
  if (step.id === stepId) return updater(step)
  if (!Object.keys(step.branches || {}).length) return step
  return { ...step, branches: Object.fromEntries(Object.entries(step.branches).map(([key, branch]) => [key, mapStepTree(branch, stepId, updater)])) }
})

const removeStepFromTree = (steps, stepId) => (steps || [])
  .filter((step) => step.id !== stepId)
  .map((step) => Object.keys(step.branches || {}).length
    ? { ...step, branches: Object.fromEntries(Object.entries(step.branches).map(([key, branch]) => [key, removeStepFromTree(branch, stepId)])) }
    : step)

const removeStepAndReturn = (steps, stepId) => {
  const list = [...(steps || [])]
  const directIndex = list.findIndex((step) => step.id === stepId)
  if (directIndex >= 0) {
    return {
      steps: [...list.slice(0, directIndex), ...list.slice(directIndex + 1)],
      removed: list[directIndex],
    }
  }
  for (let index = 0; index < list.length; index += 1) {
    const step = list[index]
    for (const [branch, branchSteps] of Object.entries(step.branches || {})) {
      const result = removeStepAndReturn(branchSteps, stepId)
      if (!result.removed) continue
      return {
        steps: list.map((item, itemIndex) => itemIndex === index
          ? { ...item, branches: { ...item.branches, [branch]: result.steps } }
          : item),
        removed: result.removed,
      }
    }
  }
  return { steps: list, removed: null }
}

const findStepLocation = (steps, stepId, containerKey = 'root') => {
  for (let index = 0; index < (steps || []).length; index += 1) {
    const step = steps[index]
    if (step.id === stepId) return { containerKey, index }
    for (const [branch, branchSteps] of Object.entries(step.branches || {})) {
      const found = findStepLocation(branchSteps, stepId, `${step.id}:${branch}`)
      if (found) return found
    }
  }
  return null
}

const stepContainsId = (step, stepId) => Boolean(step && (
  step.id === stepId
  || Object.values(step.branches || {}).some((branch) => (branch || []).some((child) => stepContainsId(child, stepId)))
))

const dropTargetKey = (target) => target?.root
  ? `root:${target.index}`
  : target
    ? `${target.stepId}:${target.branch}:${target.index}`
    : ''

const appendStepToTree = (steps, branchTarget, newStep) => {
  const list = [...(steps || [])]
  if (!branchTarget) return [...list, newStep]

  if (branchTarget.root) {
    const index = Number.isInteger(branchTarget.index)
      ? Math.max(0, Math.min(branchTarget.index, list.length))
      : list.length
    list.splice(index, 0, newStep)
    return list
  }

  return list.map((step) => {
    if (step.id === branchTarget.stepId) {
      const branch = [...(step.branches?.[branchTarget.branch] || [])]
      const index = Number.isInteger(branchTarget.index)
        ? Math.max(0, Math.min(branchTarget.index, branch.length))
        : branch.length
      branch.splice(index, 0, newStep)
      return { ...step, branches: { ...step.branches, [branchTarget.branch]: branch } }
    }
    if (!Object.keys(step.branches || {}).length) return step
    return { ...step, branches: Object.fromEntries(Object.entries(step.branches || {}).map(([key, branch]) => [key, appendStepToTree(branch, branchTarget, newStep)])) }
  })
}

const blankDefinition = () => ({
  schema_version: 1,
  steps: [],
  connections: [],
  variables: [],
  mode: 'headful',
  recipe_type: 'standard',
})

const blankRecipe = () => ({
  localKey: makeKey('recipe'),
  id: null,
  name: '',
  definition: blankDefinition(),
})

const blankStudio = () => ({
  projectId: null,
  projectName: '',
  recipes: [blankRecipe()],
})

const api = async (path, options = {}) => {
  let response
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    })
  } catch {
    throw new Error('Sui backend is offline. Start it with: python main.py')
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`)
  return body
}

function Toast({ message, onClose }) {
  const [leaving, setLeaving] = useState(false)
  const closingRef = useRef(false)

  useEffect(() => {
    let removeTimer
    const dismiss = () => {
      if (closingRef.current) return
      closingRef.current = true
      setLeaving(true)
      removeTimer = window.setTimeout(onClose, 240)
    }
    const timer = window.setTimeout(dismiss, 5000)
    return () => {
      window.clearTimeout(timer)
      if (removeTimer) window.clearTimeout(removeTimer)
    }
  }, [message])

  const dismissNow = () => {
    if (closingRef.current) return
    closingRef.current = true
    setLeaving(true)
    window.setTimeout(onClose, 240)
  }

  return <div className={`toast success-toast${leaving ? ' toast-leaving' : ''}`} role="status"><button type="button" className="toast-close" onClick={dismissNow} aria-label="Dismiss notification">×</button><span>{message}</span></div>
}

const streamEvents = async (path, onEvent) => {
  const response = await fetch(`${API_ROOT}${path}`)
  if (!response.ok || !response.body) throw new Error('Unable to connect to execution stream')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
    const messages = buffer.split('\n\n')
    buffer = messages.pop() || ''
    for (const message of messages) {
      const line = message.split('\n').find((item) => item.startsWith('data: '))
      if (line) onEvent(JSON.parse(line.slice(6)))
    }
    if (done) break
  }
}

const migrateSmartieSteps = (steps) => (steps || []).map((step) => ({
  ...step,
  settings: step.type === SMARTIE_STEP_TYPE && step.settings?.model === 'gemini-2.0-flash'
    ? { ...step.settings, model: 'gemini-2.5-flash' }
    : step.settings,
  branches: Object.keys(step.branches || {}).length
    ? Object.fromEntries(Object.entries(step.branches).map(([key, branch]) => [key, migrateSmartieSteps(branch)]))
    : step.branches,
}))

const hydrateProject = (project) => ({
  projectId: project.id,
  projectName: project.name,
  recipes: (project.recipes || []).map((recipe) => ({
    localKey: recipe.id || makeKey('recipe'),
    id: recipe.id,
    name: recipe.name,
    definition: { ...blankDefinition(), ...(recipe.definition || {}), steps: migrateSmartieSteps(recipe.definition?.steps || []) },
  })),
})

const formatTime = (value) => (value ? new Date(value).toLocaleString() : '—')

function App() {
  const [view, setView] = useState(() => window.location.pathname === '/data' ? 'data' : 'studio')
  const [projects, setProjects] = useState([])
  const [jobs, setJobs] = useState([])
  const [studio, setStudio] = useState(blankStudio)
  const [studioMode, setStudioMode] = useState('new')
  const [activeRecipeKey, setActiveRecipeKey] = useState(null)
  const [projectDetail, setProjectDetail] = useState(null)
  const [jobDetail, setJobDetail] = useState(null)
  const [jobLive, setJobLive] = useState(null)
  const [projectsSessionId] = useState(() => makeKey('projects-session'))
  const [selectedStepId, setSelectedStepId] = useState(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const activeRecipe = useMemo(
    () => studio.recipes.find((recipe) => recipe.localKey === activeRecipeKey) || studio.recipes[0] || null,
    [studio.recipes, activeRecipeKey],
  )

  useEffect(() => {
    if (!studio.recipes.some((recipe) => recipe.localKey === activeRecipeKey)) {
      setActiveRecipeKey(studio.recipes[0]?.localKey || null)
    }
  }, [studio.recipes, activeRecipeKey])

  const loadProjects = async () => setProjects((await api('/projects')).projects)
  const loadJobs = async () => setJobs((await api('/jobs')).jobs)

  useEffect(() => {
    Promise.all([loadProjects(), loadJobs()]).catch((reason) => setError(reason.message))
  }, [])

  useEffect(() => {
    const handlePopState = () => setView(window.location.pathname === '/data' ? 'data' : window.location.pathname === '/projects' ? 'projects' : window.location.pathname === '/jobs' ? 'jobs' : 'studio')
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  useEffect(() => {
    if (view !== 'jobs') return undefined
    const timer = window.setInterval(() => loadJobs().catch(() => {}), 2000)
    return () => window.clearInterval(timer)
  }, [view])

  const changeView = (nextView) => {
    setNotice('')
    setError('')
    setView(nextView)
    const nextPath = nextView === 'studio' ? '/' : `/${nextView}`
    if (window.location.pathname !== nextPath) window.history.pushState({}, '', nextPath)
    if (nextView === 'projects') loadProjects().catch((reason) => setError(reason.message))
    if (nextView === 'jobs') loadJobs().catch((reason) => setError(reason.message))
  }

  const openProject = async (project) => {
    try {
      const detail = await api(`/projects/${project.id}`)
      setProjectDetail(detail)
      setView('projects')
      setError('')
    } catch (reason) {
      setError(reason.message)
    }
  }

  const openProjectInStudio = (project) => {
    setStudio(hydrateProject(project))
    setStudioMode('edit')
    setActiveRecipeKey(project.recipes?.[0]?.id || null)
    setSelectedStepId(null)
    setNotice('')
    setError('')
    setView('studio')
  }

  const newProject = () => {
    const draft = blankStudio()
    setStudio(draft)
    setStudioMode('new')
    setActiveRecipeKey(draft.recipes[0].localKey)
    setSelectedStepId(null)
    setProjectDetail(null)
    setNotice('')
    setError('')
    setView('studio')
  }

  const updateStudio = (changes) => setStudio((current) => ({ ...current, ...changes }))

  const updateActiveRecipe = (updater) => {
    setStudio((current) => ({
      ...current,
      recipes: current.recipes.map((recipe, index) => (
        recipe.localKey === activeRecipeKey || (!activeRecipeKey && index === 0) ? updater(recipe) : recipe
      )),
    }))
  }

  const updateRecipe = (recipeKey, updater) => {
    setStudio((current) => ({
      ...current,
      recipes: current.recipes.map((recipe) => (recipe.localKey === recipeKey ? updater(recipe) : recipe)),
    }))
  }

  const deleteRecipe = async (recipe) => {
    try {
      if (recipe.id) await api(`/recipes/${recipe.id}`, { method: 'DELETE' })
      setStudio((current) => ({ ...current, recipes: current.recipes.filter((item) => item.localKey !== recipe.localKey) }))
      if (activeRecipeKey === recipe.localKey) {
        setActiveRecipeKey(null)
        setSelectedStepId(null)
      }
      await loadProjects()
      setNotice(`Deleted ${recipe.name || 'recipe'}`)
    } catch (reason) {
      setError(reason.message)
    }
  }

  const createRecipe = () => {
    const recipe = blankRecipe()
    setStudio((current) => ({ ...current, recipes: [...current.recipes, recipe] }))
    setActiveRecipeKey(recipe.localKey)
  }

  const updateStep = (stepId, updater) => {
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: {
        ...recipe.definition,
        steps: mapStepTree(recipe.definition.steps, stepId, updater),
      },
    }))
  }

  const deleteStep = (stepId) => {
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: { ...recipe.definition, steps: removeStepFromTree(recipe.definition.steps, stepId) },
    }))
    setSelectedStepId(null)
  }

  const addStepToRecipe = (step, branchTarget = null) => {
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: { ...recipe.definition, steps: appendStepToTree(recipe.definition.steps, branchTarget, step) },
    }))
    setSelectedStepId(step.id)
    return step.id
  }

  const addNavigationStep = (position = { x: 48, y: 48 }, branchTarget = null) => {
    const step = {
      id: makeKey('step'),
      type: STEP_TYPE,
      settings: { url: '' },
      connections: { next: [] },
      position,
    }
    addStepToRecipe(step, branchTarget)
  }

  const addNavigateActionStep = (position = { x: 48, y: 48 }, action = 'back', branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: NAVIGATE_ACTION_STEP_TYPE, settings: { action },
    connections: { next: [] }, position,
  }, branchTarget)

  const addClickStep = (position = { x: 48, y: 48 }, selector = '', branchTarget = null) => {
    const step = {
      id: makeKey('step'),
      type: CLICK_STEP_TYPE,
      settings: { selector, expected_url: '', destination_selector: '', wait_timeout_seconds: 15 },
      connections: { next: [] },
      position,
    }
    return addStepToRecipe(step, branchTarget)
  }

  const addFillStep = (position = { x: 48, y: 48 }, selector = '', value = '', branchTarget = null) => {
    const step = {
      id: makeKey('step'),
      type: FILL_STEP_TYPE,
      settings: { selector, value },
      connections: { next: [] },
      position,
    }
    return addStepToRecipe(step, branchTarget)
  }

  const addDelayStep = (position = { x: 48, y: 48 }, seconds = 1, branchTarget = null) => {
    const step = {
      id: makeKey('step'),
      type: DELAY_STEP_TYPE,
      settings: { seconds, until_load_completes: false },
      disabled: false,
      connections: { next: [] },
      position,
    }
    return addStepToRecipe(step, branchTarget)
  }

  const addKeypressStep = (position = { x: 48, y: 48 }, key = 'Enter', branchTarget = null) => {
    const step = {
      id: makeKey('step'),
      type: KEYPRESS_STEP_TYPE,
      settings: { key, expected_url: '', destination_selector: '', wait_timeout_seconds: 15 },
      connections: { next: [] },
      position,
    }
    return addStepToRecipe(step, branchTarget)
  }

  const addAssignStep = (position = { x: 48, y: 48 }, branchTarget = null) => {
    const step = {
      id: makeKey('step'),
      type: ASSIGN_STEP_TYPE,
      settings: { variable: '', value: '' },
      connections: { next: [] },
      position,
    }
    return addStepToRecipe(step, branchTarget)
  }

  const addMessageBoxStep = (position = { x: 48, y: 48 }, message = '', branchTarget = null) => {
    const step = {
      id: makeKey('step'),
      type: MESSAGE_BOX_STEP_TYPE,
      settings: { message },
      connections: { next: [] },
      position,
    }
    return addStepToRecipe(step, branchTarget)
  }

  const addScrapeStep = (position = { x: 48, y: 48 }, selector = '', variable = '', branchTarget = null) => {
    const step = {
      id: makeKey('step'),
      type: SCRAPE_STEP_TYPE,
      settings: { selector, variable },
      connections: { next: [] },
      position,
    }
    return addStepToRecipe(step, branchTarget)
  }

  const addPatternMatcherStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'),
    type: PATTERN_MATCHER_STEP_TYPE,
    settings: { patterns: [''], variable: '' },
    connections: { next: [] },
    position,
  }, branchTarget)

  const addSmartieStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'),
    type: SMARTIE_STEP_TYPE,
    settings: { provider: 'google_gemini', model: 'gemini-2.5-flash', system_prompt: '', user_prompt: '', output_type: 'text', output_variable: '', schema: [] },
    connections: { next: [] },
    position,
  }, branchTarget)

  const addCheckElementExistsStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: CHECK_ELEMENT_EXISTS_STEP_TYPE, settings: { selector: '', timeout_seconds: 5 },
    connections: { next: [] }, position, branches: { exists: [], not_exists: [] },
  }, branchTarget)

  const addCheckTextExistsStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: CHECK_TEXT_EXISTS_STEP_TYPE, settings: { text: '', case_insensitive: true, timeout_seconds: 5 },
    connections: { next: [] }, position, branches: { exists: [], not_exists: [] },
  }, branchTarget)

  const addIfStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: IF_STEP_TYPE, settings: { left: '', operator: '=', right: '' },
    connections: { next: [] }, position, branches: { then: [], else: [] },
  }, branchTarget)

  const addForStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: FOR_STEP_TYPE, settings: { source: 'range', iterator: 'i', start_value: 0, range_value: '10', table_mode: 'row', table_name: '', column_name: '' },
    connections: { next: [] }, position, branches: { body: [] },
  }, branchTarget)

  const addForEachStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: FOR_STEP_TYPE, settings: { source: 'table', iterator: 'row', start_value: 0, range_value: '', table_mode: 'row', table_name: '', column_name: '' },
    connections: { next: [] }, position, branches: { body: [] },
  }, branchTarget)

  const addUseJsStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: USE_JS_STEP_TYPE, settings: { code: 'return args[0];', inputs: [], output: '', expected_url: '', destination_selector: '', wait_timeout_seconds: 15 },
    connections: { next: [] }, position,
  }, branchTarget)

  const addWriterStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: WRITER_STEP_TYPE, settings: { value: '', table_name: '', columns: [{ name: 'value', type: 'TEXT', value: '', write: true }], semantic_duplicate_removal: false, duplicate_threshold: 0.88 },
    connections: { next: [] }, position,
  }, branchTarget)

  const addCallRecipeStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: CALL_RECIPE_STEP_TYPE, settings: { recipe_id: '', recipe_name: '' },
    connections: { next: [] }, position,
  }, branchTarget)

  const addInvokeRecipeStep = (position = { x: 48, y: 48 }, branchTarget = null) => addStepToRecipe({
    id: makeKey('step'), type: INVOKE_RECIPE_STEP_TYPE, settings: { recipe_id: '', recipe_name: '' },
    connections: { next: [] }, position,
  }, branchTarget)

  const persistStudio = async () => {
    setError('')
    const projectName = studio.projectName.trim()
    if (!projectName) throw new Error('Project name is required')
    let projectId = studio.projectId
    if (!projectId) {
      const created = await api('/projects', {
        method: 'POST',
        body: JSON.stringify({ name: projectName }),
      })
      projectId = created.id
    }
    const recipes = studio.recipes.filter((recipe) => recipe.name.trim())
    if (!recipes.length) throw new Error('Add a recipe name before saving')
    for (const recipe of recipes) {
      const path = recipe.id ? `/recipes/${recipe.id}` : `/projects/${projectId}/recipes`
      await api(path, {
        method: recipe.id ? 'PUT' : 'POST',
        body: JSON.stringify({ name: recipe.name.trim(), definition: recipe.definition }),
      })
    }
    const freshProject = await api(`/projects/${projectId}`)
    setStudio(hydrateProject(freshProject))
    setProjectDetail(freshProject)
    await loadProjects()
    setNotice('Saved project and recipe')
    return freshProject
  }

  const saveStudio = async () => {
    try {
      await persistStudio()
    } catch (reason) {
      setError(reason.message)
    }
  }

  const runFullRecipe = async (studioSessionId = 'studio') => {
    try {
      const project = await persistStudio()
      const recipe = project.recipes.find((item) => item.name === activeRecipe?.name)
      if (!recipe) throw new Error('Save a named recipe before running it')
      const job = await api(`/recipes/${recipe.id}/run`, { method: 'POST', body: JSON.stringify({ studio_session_id: studioSessionId }) })
      await loadJobs()
      setNotice('Recipe started')
      return job
    } catch (reason) {
      setError(reason.message)
      throw reason
    }
  }

  const stopJob = async (jobId) => {
    try {
      const job = await api(`/jobs/${jobId}/stop`, { method: 'POST' })
      await loadJobs()
      if (jobDetail?.id === jobId) setJobDetail(job)
      setJobLive((current) => current?.jobId === jobId ? { ...current, status: job.status, error: job.error || null } : current)
      setNotice('Recipe stopped')
      return job
    } catch (reason) {
      setError(reason.message)
      throw reason
    }
  }

  const runProjectRecipe = async (recipe) => {
    try {
      const job = await api(`/recipes/${recipe.id}/run`, { method: 'POST', body: JSON.stringify({ studio_session_id: projectsSessionId }) })
      await loadJobs()
      setJobDetail(job)
      setJobLive({ jobId: job.id, status: 'queued', frame: null, logs: [], error: null })
      streamEvents(`/jobs/${job.id}/events`, (event) => {
        setJobLive((current) => {
          const next = current || { jobId: job.id, frame: null, logs: [], error: null }
          if (event.type === 'frame') return { ...next, frame: event.data }
          if (event.type === 'log') return { ...next, logs: [...next.logs, event], }
          if (event.type === 'status' || event.type === 'complete') return { ...next, status: event.status, error: event.error || null }
          return next
        })
      }).catch((reason) => setError(reason.message))
      setNotice('Recipe started')
      setView('jobs')
    } catch (reason) {
      setError(reason.message)
    }
  }

  const openJob = async (job) => {
    try {
      setJobDetail(await api(`/jobs/${job.id}`))
      setJobLive({ jobId: job.id, status: job.status, frame: null, logs: [], error: job.error || null })
      if (job.status === 'queued' || job.status === 'running') {
        streamEvents(`/jobs/${job.id}/events`, (event) => {
          setJobLive((current) => {
            const next = current || { jobId: job.id, frame: null, logs: [], error: null }
            if (event.type === 'frame') return { ...next, frame: event.data }
            if (event.type === 'log') return { ...next, logs: [...next.logs, event] }
            if (event.type === 'status' || event.type === 'complete') return { ...next, status: event.status, error: event.error || null }
            return next
          })
        }).catch(() => {})
      }
      setView('jobs')
    } catch (reason) {
      setError(reason.message)
    }
  }

  return (
    <div className={`app-shell ${view}-view`}>
      <header className="topbar">
        <button className="wordmark" onClick={() => changeView('studio')} aria-label="Open Studio">
          <img className="wordmark-logo" src="/logo.png" alt="Sui" />
        </button>
        <nav className="nav-tabs" aria-label="Primary navigation">
          {['studio', 'projects', 'jobs', 'data'].map((item) => (
            <button key={item} className={view === item ? 'nav-tab active' : 'nav-tab'} onClick={() => changeView(item)}>
              {item[0].toUpperCase() + item.slice(1)}
            </button>
          ))}
        </nav>
      </header>

      <main className="main-content">
        {notice && <Toast message={notice} onClose={() => setNotice('')} />}
        {error && <div className="toast error-toast">{error}</div>}
        {view === 'studio' && (
          <StudioView
            studio={studio}
            studioMode={studioMode}
            activeRecipe={activeRecipe}
            activeRecipeKey={activeRecipeKey}
            setActiveRecipeKey={setActiveRecipeKey}
            updateRecipe={updateRecipe}
            deleteRecipe={deleteRecipe}
            selectedStepId={selectedStepId}
            setSelectedStepId={setSelectedStepId}
            updateStudio={updateStudio}
            updateActiveRecipe={updateActiveRecipe}
            updateStep={updateStep}
            deleteStep={deleteStep}
            addNavigationStep={addNavigationStep}
            addNavigateActionStep={addNavigateActionStep}
            addClickStep={addClickStep}
            addFillStep={addFillStep}
            addDelayStep={addDelayStep}
            addKeypressStep={addKeypressStep}
            addAssignStep={addAssignStep}
            addMessageBoxStep={addMessageBoxStep}
            addScrapeStep={addScrapeStep}
            addPatternMatcherStep={addPatternMatcherStep}
            addSmartieStep={addSmartieStep}
            addCheckElementExistsStep={addCheckElementExistsStep}
            addCheckTextExistsStep={addCheckTextExistsStep}
            addIfStep={addIfStep}
            addForStep={addForStep}
            addForEachStep={addForEachStep}
            addUseJsStep={addUseJsStep}
            addWriterStep={addWriterStep}
            addCallRecipeStep={addCallRecipeStep}
            addInvokeRecipeStep={addInvokeRecipeStep}
            saveStudio={saveStudio}
            runFullRecipe={runFullRecipe}
            stopJob={stopJob}
            newRecipe={createRecipe}
          />
        )}
        {view === 'data' && <DataView />}
        {view === 'projects' && (
          <ProjectsView
            projects={projects}
            projectDetail={projectDetail}
            newProject={newProject}
            openProject={openProject}
            editProject={openProjectInStudio}
            runRecipe={runProjectRecipe}
            clearDetail={() => setProjectDetail(null)}
          />
        )}
        {view === 'jobs' && <JobsView jobs={jobs} jobDetail={jobDetail} jobLive={jobLive} openJob={openJob} stopJob={stopJob} clearDetail={() => setJobDetail(null)} />}
      </main>
    </div>
  )
}

function StudioView({
  studio, studioMode, activeRecipe, activeRecipeKey, setActiveRecipeKey, updateRecipe, deleteRecipe, selectedStepId, setSelectedStepId, updateStudio, updateActiveRecipe, updateStep, deleteStep,
  addNavigationStep, addNavigateActionStep, addClickStep, addFillStep, addDelayStep, addKeypressStep, addAssignStep, addMessageBoxStep, addScrapeStep, addPatternMatcherStep, addSmartieStep, addCheckElementExistsStep, addCheckTextExistsStep, addIfStep, addForStep, addForEachStep, addUseJsStep, addWriterStep, addCallRecipeStep, addInvokeRecipeStep, saveStudio, runFullRecipe, stopJob, newRecipe,
}) {
  const [editingRecipeKey, setEditingRecipeKey] = useState(null)
  const [rightMode, setRightMode] = useState('viewer')
  const [fullRun, setFullRun] = useState({ id: null, status: 'idle' })
  const [pickerActive, setPickerActive] = useState(false)
  const [pickerTargetStepId, setPickerTargetStepId] = useState(null)
  const [pickerAction, setPickerAction] = useState('click')
  const [pickerValue, setPickerValue] = useState('')
  const [pickerTrial, setPickerTrial] = useState({ status: 'idle', selector: '', error: null, trialId: null, value: '' })
  const [pickerVariable, setPickerVariable] = useState('')
  const [newPickerVariableName, setNewPickerVariableName] = useState('')
  const [execution, setExecution] = useState({ status: 'idle', frame: null, logs: [], error: null, pickerElement: null, navigationRunId: null, navigationStatus: 'idle', sessionStatus: 'idle', sessionScopeKey: null })
  const [pendingDeleteRecipe, setPendingDeleteRecipe] = useState(null)
  const [variableName, setVariableName] = useState('')
  const [variableValue, setVariableValue] = useState('')
  const [branchTarget, setBranchTarget] = useState(null)
  const [draggedStepId, setDraggedStepId] = useState(null)
  const [dragOverTarget, setDragOverTarget] = useState('')
  const [writerTables, setWriterTables] = useState([])
  const [loopTableColumns, setLoopTableColumns] = useState([])
  const [loopSchemaLoading, setLoopSchemaLoading] = useState(false)
  const [writerSchemaLoading, setWriterSchemaLoading] = useState(false)
  const [writerSchemaError, setWriterSchemaError] = useState('')
  const [studioSessionId] = useState(() => makeKey('studio-session'))
  const inputQueueRef = useRef(Promise.resolve())
  const hoverInputBusyRef = useRef(false)
  const pendingHoverInputRef = useRef(null)
  const stepRunLockRef = useRef(false)
  const sessionStreamRef = useRef({ runId: null, cursor: 0, controller: null, stopped: false })
  const sessionStreamStartCursorRef = useRef(0)
  const pendingCommandsRef = useRef(new Map())
  const activeStepCommandIdRef = useRef(null)
  const steps = activeRecipe?.definition.steps || []
  const selectedStep = findStepInTree(steps, selectedStepId)
  const allSteps = flattenSteps(steps)
  const stepExecutionStates = buildStepExecutionStates(execution)
  const callableRecipes = studio.recipes.filter((recipe) => recipe.localKey !== activeRecipeKey && (recipe.id || recipe.name.trim()))
  const invokeRecipes = studio.recipes.filter((recipe) => recipe.id || recipe.name.trim())
  const firstStep = steps[0]
  const executionMode = activeRecipe?.definition?.mode || 'headful'
  const executionScopeKey = `${studio.projectId || 'draft-project'}:${activeRecipe?.id || activeRecipe?.localKey || 'draft-recipe'}:${studioSessionId}:${executionMode}`
  const navigationReadyToAddActivities = firstStep?.type === STEP_TYPE && Boolean(firstStep.settings?.url?.trim())
  const navigationRunCompleted = navigationReadyToAddActivities
    && execution.navigationStatus === 'completed'
    && execution.sessionStatus === 'ready'
    && execution.sessionScopeKey === executionScopeKey
    && Boolean(execution.navigationRunId)
    && !(execution.stepId === firstStep?.id && execution.status === 'running')
  const requiresInitialNavigation = activeRecipe?.definition?.recipe_type !== 'orchest'
  const activityPickerLocked = activeRecipe?.definition?.recipe_type !== 'orchest' && !navigationRunCompleted
  const variables = activeRecipe?.definition?.variables || []
  const writerColumns = selectedStep?.settings?.columns?.length
    ? selectedStep.settings.columns
    : [{ name: 'value', type: 'TEXT', value: selectedStep?.settings?.value || '', write: true }]

  const updateWriterColumns = (updater) => {
    if (!selectedStepId) return
    updateStep(selectedStepId, (current) => ({
      ...current,
      settings: { ...current.settings, columns: updater(current.settings?.columns?.length ? current.settings.columns : [{ name: 'value', type: 'TEXT', value: current.settings?.value || '', write: true }]) },
    }))
  }

  const loadWriterTables = async () => {
    try {
      const result = await api('/writer/tables')
      setWriterTables(result.tables || [])
      setWriterSchemaError('')
    } catch (reason) {
      setWriterSchemaError(reason.message)
    }
  }

  const loadWriterSchema = async (tableName = selectedStep?.settings?.table_name) => {
    const name = String(tableName || '').trim()
    if (!name || !selectedStepId) return
    setWriterSchemaLoading(true)
    setWriterSchemaError('')
    try {
      const result = await api(`/writer/schema?table=${encodeURIComponent(name)}`)
      updateStep(selectedStepId, (current) => ({
        ...current,
        settings: {
          ...current.settings,
          table_name: result.table || name,
          columns: (result.columns || []).map((column) => ({
            name: column.name,
            type: column.type || 'TEXT',
            value: '',
            write: column.write !== false,
            primary_key: Boolean(column.primary_key),
          })),
        },
      }))
    } catch (reason) {
      setWriterSchemaError(reason.message)
    } finally {
      setWriterSchemaLoading(false)
    }
  }

  const loadLoopTableSchema = async (tableName = selectedStep?.settings?.table_name) => {
    const name = String(tableName || '').trim()
    if (!name) { setLoopTableColumns([]); return }
    setLoopSchemaLoading(true)
    try {
      const result = await api(`/writer/schema?table=${encodeURIComponent(name)}`)
      setLoopTableColumns(result.columns || [])
    } catch (reason) {
      setLoopTableColumns([])
    } finally {
      setLoopSchemaLoading(false)
    }
  }

  useEffect(() => {
    if (rightMode === 'editor' && [WRITER_STEP_TYPE, FOR_STEP_TYPE].includes(selectedStep?.type)) {
      loadWriterTables()
      if (selectedStep?.type === FOR_STEP_TYPE && selectedStep.settings?.source === 'table') loadLoopTableSchema()
    }
  }, [rightMode, selectedStepId, selectedStep?.type])

  useEffect(() => {
    if (rightMode === 'editor' && selectedStep?.type === FOR_STEP_TYPE && selectedStep.settings?.source === 'table') {
      loadLoopTableSchema(selectedStep.settings?.table_name)
    }
  }, [selectedStep?.settings?.table_name, selectedStep?.settings?.source, rightMode, selectedStep?.type])

  const handleSessionEvent = (event) => {
    if (event.type === 'frame') setExecution((current) => ({ ...current, frame: event.data }))
    if (event.type === 'log') setExecution((current) => ({ ...current, logs: [...current.logs, event.message] }))
    if (event.type === 'inspect') setExecution((current) => ({ ...current, pickerElement: event.element || null }))
    if (event.type === 'status') setExecution((current) => ({ ...current, status: event.status }))
    if (event.type === 'session' && event.status === 'ready') setExecution((current) => ({ ...current, sessionStatus: 'ready' }))
    if (event.type === 'session' && event.status === 'dead') {
      stepRunLockRef.current = false
      activeStepCommandIdRef.current = null
      pendingCommandsRef.current.clear()
      setExecution((current) => ({ ...current, status: 'failed', sessionStatus: 'dead', navigationRunId: null, runId: null, navigationStatus: 'idle', error: event.error || 'Browser session ended' }))
      return
    }
    if (event.type !== 'action_complete' || !event.command_id) return
    const pending = pendingCommandsRef.current.get(event.command_id)
    if (!pending) return
    pendingCommandsRef.current.delete(event.command_id)
    const sessionLost = event.status === 'failed' && /browser session is no longer active|target or browser is closed|browser is closed|browser target closed/i.test(event.error || '')
    if (pending.kind === 'picker') {
      const value = event.action === 'check_element_exists'
        ? (event.exists ? 'Element found' : 'No matching element found')
        : event.scraped_value ?? ''
      setPickerTrial((current) => current.trialId === pending.trialId ? { ...current, status: event.status, error: event.error || null, value } : current)
    } else if (activeStepCommandIdRef.current === event.command_id) {
      activeStepCommandIdRef.current = null
      stepRunLockRef.current = false
      setExecution((current) => ({
        ...current,
        status: event.status,
        error: event.error || null,
        ...(pending.isFirstNavigation ? {
          navigationStatus: event.status,
          sessionStatus: event.status === 'completed' ? 'ready' : 'failed',
        } : {}),
        ...(sessionLost ? { sessionStatus: 'dead', navigationRunId: null, runId: null, navigationStatus: 'idle' } : {}),
      }))
    }
  }

  useEffect(() => {
    const runId = execution.navigationRunId
    if (!runId) return undefined
    if (sessionStreamRef.current.runId === runId && sessionStreamRef.current.controller) return undefined
    let stopped = false
    const startCursor = sessionStreamRef.current.runId === runId
      ? sessionStreamRef.current.cursor
      : sessionStreamStartCursorRef.current
    sessionStreamRef.current = { runId, cursor: startCursor, controller: null, stopped: false }
    const reconnect = (delay) => new Promise((resolve) => window.setTimeout(resolve, delay))
    const consume = async () => {
      while (!stopped) {
        const controller = new AbortController()
        sessionStreamRef.current.controller = controller
        try {
          const response = await fetch(`${API_ROOT}/step-runs/${runId}/events?after=${sessionStreamRef.current.cursor}`, { signal: controller.signal })
          if (response.status === 404) {
            stopped = true
            handleSessionEvent({ type: 'session', status: 'dead', error: 'Execution session ended' })
            return
          }
          if (!response.ok || !response.body) throw new Error('Execution stream unavailable')
          const reader = response.body.getReader()
          const decoder = new TextDecoder()
          let buffer = ''
          while (!stopped) {
            const { value, done } = await reader.read()
            buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
            const messages = buffer.split('\n\n')
            buffer = messages.pop() || ''
            for (const message of messages) {
              const idLine = message.split('\n').find((line) => line.startsWith('id: '))
              const dataLine = message.split('\n').find((line) => line.startsWith('data: '))
              if (idLine) sessionStreamRef.current.cursor = Number(idLine.slice(4)) + 1
              if (dataLine) handleSessionEvent(JSON.parse(dataLine.slice(6)))
            }
            if (done) break
          }
          if (!stopped) await reconnect(300)
        } catch (reason) {
          if (!stopped && reason.name !== 'AbortError') await reconnect(500)
        } finally {
          if (sessionStreamRef.current.controller === controller) sessionStreamRef.current.controller = null
        }
      }
    }
    consume()
    return () => {
      stopped = true
      if (sessionStreamRef.current.runId === runId) {
        sessionStreamRef.current.stopped = true
        sessionStreamRef.current.controller?.abort()
        sessionStreamRef.current.controller = null
      }
    }
  }, [execution.navigationRunId])

  const openRecipeConfig = (recipeKey = activeRecipeKey) => {
    setActiveRecipeKey(recipeKey)
    setSelectedStepId(null)
    setPickerActive(false)
    setRightMode('config')
  }

  const createVariable = () => {
    const name = variableName.trim()
    if (!name) return
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return
    if (variables.some((variable) => variable.name === name)) return
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: {
        ...recipe.definition,
        variables: [...(recipe.definition.variables || []), { id: makeKey('variable'), name, value: variableValue }],
      },
    }))
    setVariableName('')
    setVariableValue('')
  }

  const updateVariable = (variableId, changes) => {
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: {
        ...recipe.definition,
        variables: (recipe.definition.variables || []).map((variable) => variable.id === variableId ? { ...variable, ...changes } : variable),
      },
    }))
  }

  const removeVariable = (variableId) => {
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: {
        ...recipe.definition,
        variables: (recipe.definition.variables || []).filter((variable) => variable.id !== variableId),
      },
    }))
  }

  const runStepLive = async (step) => {
    if (step.disabled) return
    const isFirstNavigation = step.id === firstStep?.id && step.type === STEP_TYPE
    const isOrchestRecipe = activeRecipe?.definition?.recipe_type === 'orchest'
    if (!isFirstNavigation && !navigationRunCompleted && !isOrchestRecipe) return
    for (const [commandId, pending] of pendingCommandsRef.current.entries()) {
      if (pending.kind === 'step') pendingCommandsRef.current.delete(commandId)
    }
    activeStepCommandIdRef.current = null
    stepRunLockRef.current = true
    sendViewerInput({ type: 'clear_picker_overlay' })
    setRightMode('viewer')
    setPickerActive(false)
    const isFreshSession = execution.sessionStatus !== 'ready'
    setExecution((current) => ({
      ...current,
      status: 'running',
      frame: isFreshSession ? null : current.frame,
      logs: isFreshSession ? [] : current.logs,
      error: null,
      stepId: step.id,
      stepType: step.type,
      sessionStatus: isFirstNavigation ? 'starting' : current.sessionStatus,
      navigationStatus: isFirstNavigation ? 'running' : current.navigationStatus,
      sessionScopeKey: executionScopeKey,
    }))
    try {
      let startResponse
      try {
        startResponse = await fetch(`${API_ROOT}/step-runs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            step,
            project_id: studio.projectId || 'draft-project',
            recipe_id: activeRecipe?.id || activeRecipe?.localKey || 'draft-recipe',
            studio_session_id: studioSessionId,
            mode: executionMode,
            recipe_type: activeRecipe?.definition?.recipe_type || 'standard',
            variables,
          }),
        })
      } catch {
        throw new Error('Sui backend is offline. Start it with: python main.py')
      }
      const started = await startResponse.json()
      if (!startResponse.ok) throw new Error(started.error || 'Unable to start step')
      pendingCommandsRef.current.set(started.command_id, { kind: 'step', isFirstNavigation })
      activeStepCommandIdRef.current = started.command_id
      if (!sessionStreamRef.current.runId || sessionStreamRef.current.runId !== started.run_id) {
        sessionStreamStartCursorRef.current = Number(started.event_index || 0)
      }
      setExecution((current) => ({ ...current, runId: started.run_id, navigationRunId: started.run_id }))
    } catch (reason) {
      setExecution((current) => ({ ...current, status: 'failed', error: reason.message, ...(isFirstNavigation ? { navigationStatus: 'failed', sessionStatus: 'failed' } : {}) }))
      stepRunLockRef.current = false
    } finally {
      // The persistent session stream releases this lock only for the matching command_id.
    }
  }

  const sendViewerInput = (payload) => {
    const browserRunId = execution.navigationRunId || execution.runId
    if (!browserRunId) return
    if (payload.type === 'inspect_move' || payload.type === 'clear_picker_overlay') {
      pendingHoverInputRef.current = payload
      if (hoverInputBusyRef.current) return
      hoverInputBusyRef.current = true
      const sendHover = (nextPayload) => api(`/step-runs/${browserRunId}/input`, {
        method: 'POST',
        body: JSON.stringify(nextPayload),
      }).catch((reason) => {
        if (/browser session is no longer active|target or browser is closed|browser is closed/i.test(reason.message || '')) {
          setExecution((current) => ({ ...current, status: 'failed', sessionStatus: 'dead', navigationRunId: null, runId: null, navigationStatus: 'idle', error: reason.message }))
        }
      })
      const flushHover = (activePayload) => sendHover(activePayload).finally(() => {
        const next = pendingHoverInputRef.current
        pendingHoverInputRef.current = null
        if (next && next !== activePayload) flushHover(next)
        else hoverInputBusyRef.current = false
      })
      flushHover(payload)
      return
    }
    inputQueueRef.current = inputQueueRef.current
      .then(() => api(`/step-runs/${browserRunId}/input`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }))
      .catch((reason) => {
        if (/browser session is no longer active|target or browser is closed|browser is closed/i.test(reason.message || '')) {
          setExecution((current) => ({ ...current, status: 'failed', sessionStatus: 'dead', navigationRunId: null, runId: null, navigationStatus: 'idle', error: reason.message }))
        } else {
          setExecution((current) => ({ ...current, error: reason.message }))
        }
      })
  }

  const selectStep = (stepId) => {
    sendViewerInput({ type: 'clear_picker_overlay' })
    setPickerActive(false)
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null })
    setSelectedStepId(stepId)
    setRightMode('editor')
  }
  const activatePicker = (targetStepId = null, action = 'click') => {
    if (activeRecipe?.definition?.recipe_type !== 'orchest' && !navigationRunCompleted) return
    setRightMode('viewer')
    setPickerActive(true)
    setPickerTargetStepId(targetStepId)
    setPickerAction(action)
    const target = targetStepId ? findStepInTree(steps, targetStepId) : null
    setPickerValue(target?.settings?.value || '')
    setPickerVariable(target?.settings?.variable || (action === 'scrape' ? variables[0]?.name || '' : ''))
    setNewPickerVariableName('')
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null, value: '' })
    setExecution((current) => ({ ...current, pickerElement: null, error: null }))
  }
  const beginActivityPicker = (action) => {
    if (activeRecipe?.definition?.recipe_type !== 'orchest' && !navigationReadyToAddActivities) return
    if (activeRecipe?.definition?.recipe_type === 'orchest' || navigationRunCompleted) activatePicker(null, action)
  }
  const choosePickerSelector = (selector) => {
    const value = String(selector || '').trim()
    if (!value) return
    setExecution((current) => ({ ...current, pickerElement: current.pickerElement ? { ...current.pickerElement, selectedSelector: value } : current.pickerElement }))
    setPickerTrial({ status: 'idle', selector: value, error: null, trialId: null, value: '' })
  }
  const repickElement = () => {
    sendViewerInput({ type: 'clear_picker_overlay' })
    setExecution((current) => ({ ...current, pickerElement: null }))
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null, value: '' })
    setPickerActive(true)
  }
  const tryPickerSelector = async () => {
    const selector = String(execution.pickerElement?.selectedSelector || execution.pickerElement?.selector || '').trim()
    const browserRunId = execution.navigationRunId || execution.runId
    if (!selector || !browserRunId) return
    const trialId = makeKey('picker-trial')
    setPickerTrial({ status: 'running', selector, error: null, trialId, value: '' })
    setPickerActive(false)
    try {
      const started = await api(`/step-runs/${browserRunId}/input`, {
        method: 'POST',
        body: JSON.stringify(pickerAction === 'check_element_exists'
          ? { type: 'selector_check', selector, trial_id: trialId }
          : pickerAction === 'fill'
          ? { type: 'selector_fill', selector, value: pickerValue, trial_id: trialId }
          : pickerAction === 'scrape'
            ? { type: 'selector_scrape', selector, trial_id: trialId }
            : { type: 'selector_click', selector, trial_id: trialId }),
      })
      pendingCommandsRef.current.set(started.command_id, { kind: 'picker', trialId })
    } catch (reason) {
      setPickerTrial({ status: 'failed', selector, error: reason.message, trialId, value: '' })
    }
  }
  const createPickerVariable = () => {
    const name = newPickerVariableName.trim()
    if (!name || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name) || variables.some((variable) => variable.name === name)) return
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: { ...recipe.definition, variables: [...(recipe.definition.variables || []), { id: makeKey('variable'), name, value: '' }] },
    }))
    setPickerVariable(name)
    setNewPickerVariableName('')
  }
  const cancelPicker = () => {
    sendViewerInput({ type: 'clear_picker_overlay' })
    setPickerActive(false)
    setPickerTargetStepId(null)
    setPickerAction('click')
    setPickerValue('')
    setPickerVariable('')
    setNewPickerVariableName('')
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null, value: '' })
    setExecution((current) => ({ ...current, pickerElement: null, error: null }))
    setRightMode('viewer')
  }

  const startFullRecipe = async () => {
    if (fullRun.id && ['queued', 'running'].includes(fullRun.status)) {
      try {
        const stopped = await stopJob(fullRun.id)
        setFullRun({ id: stopped.id, status: stopped.status })
        setExecution((current) => ({ ...current, status: stopped.status, error: stopped.error || null }))
      } catch (reason) {
        setExecution((current) => ({ ...current, error: reason.message }))
      }
      return
    }
    setRightMode('viewer')
    setExecution((current) => ({ ...current, status: 'running', frame: null, logs: [], error: null, stepId: null, stepType: null }))
    try {
      const job = await runFullRecipe(studioSessionId)
      if (!job?.id) return
      setFullRun({ id: job.id, status: job.status || 'queued' })
      await streamEvents(`/jobs/${job.id}/events`, (event) => {
        if (event.type === 'frame') setExecution((current) => ({ ...current, frame: event.data }))
        if (event.type === 'log') setExecution((current) => ({ ...current, logs: [...current.logs, event.message] }))
        if (event.type === 'status' || event.type === 'complete') {
          setFullRun((current) => ({ ...current, id: job.id, status: event.status }))
          setExecution((current) => ({ ...current, status: event.status, error: event.error || null }))
        }
      })
      setFullRun((current) => (['queued', 'running'].includes(current.status) ? { ...current, status: 'completed' } : current))
    } catch (reason) {
      setFullRun((current) => ({ ...current, status: 'failed' }))
      setExecution((current) => ({ ...current, status: 'failed', error: reason.message }))
    }
  }
  const confirmPickedActivity = () => {
    const selector = execution.pickerElement?.selectedSelector || execution.pickerElement?.selector
    if (!selector) return
    sendViewerInput({ type: 'clear_picker_overlay' })
    const targetStep = findStepInTree(steps, pickerTargetStepId)
    if (pickerAction === 'scrape' && !pickerVariable) return
    const targetType = pickerAction === 'fill' ? FILL_STEP_TYPE : pickerAction === 'scrape' ? SCRAPE_STEP_TYPE : pickerAction === 'check_element_exists' ? CHECK_ELEMENT_EXISTS_STEP_TYPE : CLICK_STEP_TYPE
    const targetStepId = targetStep?.type === targetType
      ? targetStep.id
      : pickerAction === 'fill' ? addFillStep({ x: 48, y: 48 }, '', '', branchTarget) : pickerAction === 'scrape' ? addScrapeStep({ x: 48, y: 48 }, selector, pickerVariable, branchTarget) : pickerAction === 'check_element_exists' ? addCheckElementExistsStep({ x: 48, y: 48 }, branchTarget) : addClickStep({ x: 48, y: 48 }, '', branchTarget)
    updateStep(targetStepId, (current) => ({
      ...current,
      settings: { ...current.settings, selector, ...(pickerAction === 'fill' ? { value: pickerValue } : {}), ...(pickerAction === 'scrape' ? { variable: pickerVariable } : {}) },
    }))
    setPickerActive(false)
    setPickerTargetStepId(null)
    setPickerAction('click')
    setPickerValue('')
    setPickerVariable('')
    setNewPickerVariableName('')
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null, value: '' })
    setRightMode('editor')
  }

  const toggleBranchTarget = (stepId, branch) => {
    setBranchTarget((current) => current?.stepId === stepId && current.branch === branch ? null : { stepId, branch })
    setRightMode('picker')
  }

  const toggleStepDisabled = (stepId) => {
    updateStep(stepId, (current) => ({ ...current, disabled: !current.disabled }))
  }

  const startStepDrag = (event, step) => {
    if (step.id === firstStep?.id && step.type === STEP_TYPE) {
      event.preventDefault()
      return
    }
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('application/x-sui-step', step.id)
    event.dataTransfer.setData('text/plain', step.id)
    setDraggedStepId(step.id)
  }

  const finishStepDrag = () => {
    setDraggedStepId(null)
    setDragOverTarget('')
  }

  const handleStepDrop = (event, target) => {
    event.preventDefault()
    event.stopPropagation()
    const stepId = event.dataTransfer.getData('application/x-sui-step') || draggedStepId
    if (!stepId) return finishStepDrag()
    const sourceStep = findStepInTree(steps, stepId)
    if (!sourceStep || (!target?.root && stepContainsId(sourceStep, target.stepId))) return finishStepDrag()

    updateActiveRecipe((recipe) => {
      const sourceSteps = recipe.definition.steps || []
      const sourceLocation = findStepLocation(sourceSteps, stepId)
      const removal = removeStepAndReturn(sourceSteps, stepId)
      if (!removal.removed) return recipe
      const insertionTarget = { ...target }
      if (sourceLocation?.containerKey === (target.root ? 'root' : `${target.stepId}:${target.branch}`)
        && sourceLocation.index < insertionTarget.index) {
        insertionTarget.index -= 1
      }
      if (insertionTarget.root) insertionTarget.index = Math.max(1, insertionTarget.index)
      return {
        ...recipe,
        definition: {
          ...recipe.definition,
          steps: appendStepToTree(removal.steps, insertionTarget, removal.removed),
        },
      }
    })
    setSelectedStepId(stepId)
    setRightMode('editor')
    finishStepDrag()
  }

  const allowStepDrop = (event, target) => {
    event.preventDefault()
    event.stopPropagation()
    event.dataTransfer.dropEffect = 'move'
    setDragOverTarget(dropTargetKey(target))
  }

  const renderStepList = (items, depth = 0, branchContext = null) => (items || []).map((step, index) => {
    const stepLocked = Boolean(step.disabled) || (requiresInitialNavigation && !(step.id === firstStep?.id && step.type === STEP_TYPE) && !navigationRunCompleted)
    const stepDraggable = !(step.id === firstStep?.id && step.type === STEP_TYPE)
    const stepExecution = stepExecutionStates[step.id]
    const executionClass = stepExecution?.status ? ` execution-${stepExecution.status}` : ''
    return (
      <React.Fragment key={step.id}>
        <div className={`${selectedStepId === step.id ? 'step-list-item selected' : 'step-list-item'}${executionClass}${stepLocked ? ' locked' : ''}${step.disabled ? ' disabled-step' : ''}${depth ? ' nested-step' : ''}${draggedStepId === step.id ? ' dragging' : ''}`} title={stepExecution?.status === 'failed' ? stepExecution.error || 'Step failed' : undefined} role="button" tabIndex="0" draggable={stepDraggable} onDragStart={(event) => startStepDrag(event, step)} onDragEnd={finishStepDrag} onClick={(event) => {
          if (event.target.closest('.step-name') || (step.disabled && !event.target.closest('button'))) selectStep(step.id)
          else if (!event.target.closest('button')) setRightMode('viewer')
        }} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') selectStep(step.id) }}>
          <span className="step-number">{index + 1}</span>
          <span className="step-name">{stepLabel(step)}</span>
          <button className="step-play" aria-label="Run this step" disabled={stepLocked} onClick={(event) => { event.stopPropagation(); runStepLive(step) }}>▶</button>
        </div>
        {BRANCH_STEP_TYPES.includes(step.type) && (
          <div className="branch-groups">
            {branchNamesForStep(step).map(({ key, label }) => (
              <div className="branch-group" key={`${step.id}-${key}`}>
                <button className={branchTarget?.stepId === step.id && branchTarget.branch === key ? 'branch-toggle active' : 'branch-toggle'} type="button" onClick={() => toggleBranchTarget(step.id, key)}>{label} <span>{(step.branches?.[key] || []).length}</span></button>
                <div className="branch-steps">{renderStepList(step.branches?.[key] || [], depth + 1, { stepId: step.id, branch: key })}</div>
                {!(step.branches?.[key] || []).length && <button className={`branch-add-step${dragOverTarget === dropTargetKey({ stepId: step.id, branch: key, index: 0 }) ? ' drop-active' : ''}`} type="button" onClick={() => { setBranchTarget({ stepId: step.id, branch: key, index: 0 }); setRightMode('picker') }} onDragOver={(event) => allowStepDrop(event, { stepId: step.id, branch: key, index: 0 })} onDrop={(event) => handleStepDrop(event, { stepId: step.id, branch: key, index: 0 })} aria-label={`Add step to ${label}`}>+</button>}
              </div>
            ))}
          </div>
        )}
        {branchContext && <button className={`branch-add-step${dragOverTarget === dropTargetKey({ ...branchContext, index: index + 1 }) ? ' drop-active' : ''}`} type="button" onClick={() => { setBranchTarget({ ...branchContext, index: index + 1 }); setRightMode('picker') }} onDragOver={(event) => allowStepDrop(event, { ...branchContext, index: index + 1 })} onDrop={(event) => handleStepDrop(event, { ...branchContext, index: index + 1 })} aria-label="Add step after this branch step">+</button>}
        {!branchContext && <button className={`sequence-add-step${dragOverTarget === dropTargetKey({ root: true, index: index + 1 }) ? ' drop-active' : ''}`} type="button" onClick={() => { setBranchTarget({ root: true, index: index + 1 }); setRightMode('picker') }} onDragOver={(event) => allowStepDrop(event, { root: true, index: index + 1 })} onDrop={(event) => handleStepDrop(event, { root: true, index: index + 1 })} aria-label="Insert step after this project step">+</button>}
      </React.Fragment>
    )
  })

  return (
    <section className="studio-layout">
      <aside className="studio-sidebar">
        <div className="eyebrow">Studio</div>
        <div className="section-label">{studioMode === 'edit' ? 'EDIT PROJECT' : 'NEW PROJECT'}</div>
        <input aria-label="Project name" value={studio.projectName} onChange={(event) => updateStudio({ projectName: event.target.value })} placeholder="sui-dev" />
        <div className="section-label">Recipes</div>
        {studio.recipes.map((recipe, index) => (
          <div className={recipe.localKey === activeRecipeKey ? 'recipe-row active' : 'recipe-row'} key={recipe.localKey} onClick={() => { setActiveRecipeKey(recipe.localKey); setSelectedStepId(null); setRightMode('viewer') }}>
            {editingRecipeKey === recipe.localKey ? (
              <input
                autoFocus
                value={recipe.name}
                onChange={(event) => updateRecipe(recipe.localKey, (current) => ({ ...current, name: event.target.value }))}
                onBlur={() => setEditingRecipeKey(null)}
                onKeyDown={(event) => { if (event.key === 'Enter' || event.key === 'Escape') event.currentTarget.blur() }}
                placeholder={index === 0 ? 'dev-recipe' : 'recipe name'}
                onClick={(event) => event.stopPropagation()}
              />
            ) : (
              <span className="recipe-name" onDoubleClick={(event) => { event.stopPropagation(); setEditingRecipeKey(recipe.localKey) }}>
                {recipe.name || 'Untitled recipe'}
              </span>
            )}
            <button className="recipe-config-button" type="button" title="Configure recipe" aria-label={`Configure ${recipe.name || 'recipe'}`} onClick={(event) => { event.stopPropagation(); openRecipeConfig(recipe.localKey) }}>⚙</button>
            <button className="delete-button" title="Delete recipe" onClick={(event) => { event.stopPropagation(); setPendingDeleteRecipe(recipe) }}>×</button>
          </div>
        ))}
        {!studio.recipes.length && <div className="empty-sidebar">No recipes</div>}
        <button className="text-button" onClick={newRecipe}>+ New recipe</button>
        <div className="sidebar-actions">
          <button className="primary-button" onClick={saveStudio}>Save</button>
          <button className={fullRun.id && ['queued', 'running'].includes(fullRun.status) ? 'stop-run-button' : 'secondary-button'} onClick={startFullRecipe} disabled={!(fullRun.id && ['queued', 'running'].includes(fullRun.status)) && !activeRecipe?.id && !studio.projectId}>{fullRun.id && ['queued', 'running'].includes(fullRun.status) ? 'Stop full recipe' : 'Run full recipe'}</button>
        </div>
      </aside>

      <div className="studio-workspace">
        <div className="canvas-shell">
          <div className="palette">
            <div className="section-label">Blocks</div>
            {false && steps.map((step, index) => {
              const stepLocked = requiresInitialNavigation && !(step.id === firstStep?.id && step.type === STEP_TYPE) && !navigationRunCompleted
              return (
              <div
                className={`${selectedStepId === step.id ? 'step-list-item selected' : 'step-list-item'}${stepLocked ? ' locked' : ''}`}
                key={step.id}
                role="button"
                tabIndex="0"
                onClick={(event) => {
                  if (event.target.closest('.step-name')) selectStep(step.id)
                  else if (!event.target.closest('button')) setRightMode('viewer')
                }}
                onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') selectStep(step.id) }}
              >
                <span className="step-number">{index + 1}</span>
                <span className="step-name">{stepLabel(step)}</span>
                <button className="step-play" aria-label="Run this step" disabled={stepLocked} onClick={(event) => { event.stopPropagation(); runStepLive(step) }}>▶</button>
              </div>
              )
            })}
            {renderStepList(steps)}
            {!steps.length && <div className="empty-sidebar">No steps yet</div>}
            <button className="secondary-button add-button" onClick={() => { setBranchTarget(null); setRightMode('picker') }}>Add step</button>
          </div>
          <div className="canvas">
            {rightMode === 'viewer' && (
              <div className={pickerActive ? 'viewer-mode-shell picker-mode' : 'viewer-mode-shell'}>
                {pickerActive && (
                  <SelectorPicker
                    execution={execution}
                    pickerTrial={pickerTrial}
                    pickerAction={pickerAction}
                    pickerValue={pickerValue}
                    variables={variables}
                    pickerVariable={pickerVariable}
                    newVariableName={newPickerVariableName}
                    onChooseSelector={choosePickerSelector}
                    onChooseAction={(action) => {
                      const selectedSelector = execution.pickerElement?.selectedSelector || execution.pickerElement?.selector || ''
                      setPickerAction(action)
                      setPickerValue(action === 'fill' ? pickerValue : '')
                      setPickerVariable(action === 'scrape' ? pickerVariable || variables[0]?.name || '' : '')
                      setPickerTrial({ status: 'idle', selector: selectedSelector, error: null, trialId: null, value: '' })
                      setPickerActive(true)
                    }}
                    onChangeValue={setPickerValue}
                    onChangeVariable={setPickerVariable}
                    onChangeNewVariable={setNewPickerVariableName}
                    onCreateVariable={createPickerVariable}
                    onTrySelector={tryPickerSelector}
                    onRepick={repickElement}
                    onConfirm={confirmPickedActivity}
                    onCancel={cancelPicker}
                  />
                )}
                {!pickerActive && pickerTrial.status !== 'idle' && execution.pickerElement && (
                  <div className="picker-review-bar">
                    <button className="back-to-selectors" type="button" onClick={() => setPickerActive(true)}>BACK TO SELECTORS</button>
                    <span>{pickerTrial.status === 'running' ? 'Running trial…' : pickerTrial.status === 'completed' ? 'Trial completed' : 'Trial failed'}</span>
                  </div>
                )}
                {(!pickerActive || !execution.pickerElement) && (
                  <ExecutionViewer
                    execution={execution}
                    steps={steps}
                    onInput={sendViewerInput}
                    pickerActive={pickerActive}
                    pickerEnabled={navigationRunCompleted || activeRecipe?.definition?.recipe_type === 'orchest'}
                    onPickerToggle={() => activatePicker()}
                  />
                )}
              </div>
            )}
            {rightMode === 'picker' && (
              <div className="step-picker">
                <button className="back-execution" onClick={() => setRightMode('viewer')}>← Back to execution</button>
                <div className="section-label">Available activities</div>
                {branchTarget && (branchTarget.root
                  ? <div className="branch-target-note">Inserting after project step {branchTarget.index}</div>
                  : <div className="branch-target-note">Adding under: {stepLabel(findStepInTree(steps, branchTarget.stepId))} / {branchNamesForStep(findStepInTree(steps, branchTarget.stepId)).find((item) => item.key === branchTarget.branch)?.label}</div>)}
                <button className="picker-option" disabled={Boolean(steps.length && firstStep?.type !== STEP_TYPE) || Boolean(branchTarget)} onClick={() => { addNavigationStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }}>
                  <span className="block-icon">↗</span><span>Navigate to website</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addNavigateActionStep({ x: 48, y: 48 }, 'back', branchTarget); setRightMode('editor') }} title="Go back, go forward, or refresh the current page">
                  <span className="block-icon">↔</span><span>Navigate</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => beginActivityPicker('click')} title={!navigationReadyToAddActivities ? 'Add a URL to the first Navigate to website step first' : 'Run the first Navigate to website step before picking an element'}>
                  <span className="block-icon">+</span><span>Click</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => beginActivityPicker('fill')} title="Run the first Navigate to website step before picking an element">
                  <span className="block-icon">+</span><span>Fill</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addDelayStep({ x: 48, y: 48 }, 1, branchTarget); setRightMode('editor') }} title="Run the first Navigate to website step before adding a delay">
                  <span className="block-icon">+</span><span>Delay</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addKeypressStep({ x: 48, y: 48 }, 'Enter', branchTarget); setRightMode('editor') }} title="Send a keyboard key to the browser">
                  <span className="block-icon">+</span><span>Keypress</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked || (!variables.length && activeRecipe?.definition?.recipe_type !== 'orchest')} onClick={() => { addAssignStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title={!variables.length ? 'Create a variable in recipe configuration first' : 'Assign a recipe variable'}>
                  <span className="block-icon">+</span><span>Assign</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => beginActivityPicker('scrape')} title="Pick an element and store its text in a recipe variable">
                  <span className="block-icon">+</span><span>Scrape</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addPatternMatcherStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title="Extract the first match from text using one or more regex patterns">
                  <span className="block-icon">.*</span><span>Pattern Matcher</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addSmartieStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title="Use Google Gemini with prompts and a structured output">
                  <span className="block-icon">AI</span><span>Smartie</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addMessageBoxStep({ x: 48, y: 48 }, '', branchTarget); setRightMode('editor') }} title="Show a message with an OK button">
                  <span className="block-icon">+</span><span>Message box</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addCheckElementExistsStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }}>
                  <span className="block-icon">?</span><span>Check element exists</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addCheckTextExistsStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }}>
                  <span className="block-icon">?</span><span>Check text exists</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addIfStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }}>
                  <span className="block-icon">↳</span><span>If / else</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addForStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title="Repeat nested steps for a fixed count or recipe variable">
                  <span className="block-icon">#</span><span>For loop</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addForEachStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title="Read each row or column value from a local SQLite table">
                  <span className="block-icon">↻</span><span>For each</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addUseJsStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title="Run a JavaScript function body with optional recipe variables">
                  <span className="block-icon">JS</span><span>Use JS</span>
                </button>
                <button className="picker-option" disabled={activityPickerLocked} onClick={() => { addWriterStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title="Append a value to a local SQLite table">
                  <span className="block-icon">DB</span><span>Writer</span>
                </button>
                {activeRecipe?.definition?.recipe_type === 'orchest' && <button className="picker-option" disabled={activityPickerLocked || !callableRecipes.length} onClick={() => { addCallRecipeStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title={!callableRecipes.length ? 'Create another recipe in this project first' : 'Run another recipe in this browser session'}>
                  <span className="block-icon">↗</span><span>Run recipe</span>
                </button>}
                {activeRecipe?.definition?.recipe_type === 'orchest' && <button className="picker-option" disabled={activityPickerLocked || !invokeRecipes.length} onClick={() => { addInvokeRecipeStep({ x: 48, y: 48 }, branchTarget); setRightMode('editor') }} title="Invoke a selected recipe in sequence">
                  <span className="block-icon">→</span><span>Invoke</span>
                </button>}
                <button className="back-to-view" type="button" onClick={() => setRightMode('viewer')}>BACK TO VIEW</button>
              </div>
            )}
            {rightMode === 'editor' && selectedStep && (
              <div className="step-detail">
                <div className="section-label">Selected step</div>
                <div className="step-detail-heading"><h3>{stepLabel(selectedStep)}</h3><div className="step-detail-actions"><button className="step-toggle" type="button" onClick={() => toggleStepDisabled(selectedStep.id)}>{selectedStep.disabled ? 'Enable step' : 'Disable step'}</button><button className="step-delete" type="button" onClick={() => { deleteStep(selectedStep.id); setRightMode('viewer') }}>Delete step</button></div></div>
                {selectedStep.type === CLICK_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Choose an element in the live browser. This step will click it by selector.</p>
                    <label>Selector<input value={selectedStep.settings?.selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, selector: event.target.value } }))} placeholder="Pick an element in the TV" /></label>
                    <button className="secondary-button picker-launch" disabled={!navigationRunCompleted} onClick={() => activatePicker(selectedStep.id, 'click')}>Pick element in TV</button>
                    <label>Expected URL (optional)<input value={selectedStep.settings?.expected_url || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, expected_url: event.target.value } }))} placeholder="https://example.com/detail" /></label>
                    <label>Destination selector (optional)<input value={selectedStep.settings?.destination_selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, destination_selector: event.target.value } }))} placeholder="Selector that appears after the click" /></label>
                    <label>Wait timeout (seconds)<input type="number" min="0.1" max="120" step="0.1" value={selectedStep.settings?.wait_timeout_seconds ?? 15} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, wait_timeout_seconds: event.target.value } }))} /></label>
                  </>
                ) : selectedStep.type === FILL_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Choose an element in the live browser and fill it with the value below.</p>
                    <label>Selector<input value={selectedStep.settings?.selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, selector: event.target.value } }))} placeholder="Pick an element in the TV" /></label>
                    <label>Value<input value={selectedStep.settings?.value || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, value: event.target.value } }))} placeholder="Text to fill" /></label>
                    <button className="secondary-button picker-launch" disabled={!navigationRunCompleted} onClick={() => activatePicker(selectedStep.id, 'fill')}>Pick element in TV</button>
                  </>
                ) : selectedStep.type === DELAY_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Wait for the page to finish loading, or pause for a configured number of seconds.</p>
                    <label className="check-option"><input type="checkbox" checked={selectedStep.settings?.until_load_completes === true} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, until_load_completes: event.target.checked } }))} /> Until Load Completes</label>
                    {!selectedStep.settings?.until_load_completes && <label>Seconds<input type="number" min="0.1" max="3600" step="0.1" value={selectedStep.settings?.seconds ?? 1} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, seconds: event.target.value } }))} /></label>}
                  </>
                ) : selectedStep.type === NAVIGATE_ACTION_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Control the current webpage without changing the Navigate to website step.</p>
                    <label>Action<select value={selectedStep.settings?.action || 'back'} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, action: event.target.value } }))}>
                      {NAVIGATE_ACTIONS.map((action) => <option key={action.value} value={action.value}>{action.label}</option>)}
                    </select></label>
                  </>
                ) : selectedStep.type === KEYPRESS_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Send one keyboard key to the live browser.</p>
                    <label>Key<select value={selectedStep.settings?.key || 'Enter'} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, key: event.target.value } }))}>
                      {KEYPRESS_OPTIONS.map((key) => <option key={key} value={key}>{key === 'Escape' ? 'Esc' : key}</option>)}
                    </select></label>
                    <label>Expected URL (optional)<input value={selectedStep.settings?.expected_url || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, expected_url: event.target.value } }))} placeholder="https://example.com/detail" /></label>
                    <label>Destination selector (optional)<input value={selectedStep.settings?.destination_selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, destination_selector: event.target.value } }))} placeholder="Selector that appears after the keypress" /></label>
                    <label>Wait timeout (seconds)<input type="number" min="0.1" max="120" step="0.1" value={selectedStep.settings?.wait_timeout_seconds ?? 15} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, wait_timeout_seconds: event.target.value } }))} /></label>
                  </>
                ) : selectedStep.type === ASSIGN_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Set a recipe variable before the next step runs.</p>
                    {variables.length ? (
                      <>
                        <label>Variable<select value={selectedStep.settings?.variable || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, variable: event.target.value } }))}>
                          <option value="">Choose a variable</option>
                          {variables.map((variable) => <option key={variable.id} value={variable.name}>{variable.name}</option>)}
                        </select></label>
                        <label>Value<input value={selectedStep.settings?.value || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, value: event.target.value } }))} placeholder="Value or {{variable_name}}" /></label>
                      </>
                    ) : <div className="empty-sidebar">Create a variable in Config first.</div>}
                  </>
                ) : selectedStep.type === SCRAPE_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Pick an element and store its text in a recipe variable.</p>
                    <label>Selector<input value={selectedStep.settings?.selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, selector: event.target.value } }))} placeholder="Pick an element in the TV" /></label>
                    <label>Variable<select value={selectedStep.settings?.variable || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, variable: event.target.value } }))}>
                      <option value="">Choose a variable</option>
                      {variables.map((variable) => <option key={variable.id} value={variable.name}>{variable.name}</option>)}
                    </select></label>
                    <button className="secondary-button picker-launch" disabled={!navigationRunCompleted} onClick={() => activatePicker(selectedStep.id, 'scrape')}>Pick element in TV</button>
                  </>
                ) : selectedStep.type === PATTERN_MATCHER_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Scan the currently active page text with these OR-joined regular expressions. All matches are stored in page order as one string separated by <code> | </code>.</p>
                    <label>Store match in<select value={selectedStep.settings?.variable || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, variable: event.target.value } }))}>
                      <option value="">Choose a variable</option>
                      {variables.map((variable) => <option key={variable.id} value={variable.name}>{variable.name}</option>)}
                    </select></label>
                    <div className="section-label">Regex patterns (OR)</div>
                    <div className="pattern-list">
                      {(selectedStep.settings?.patterns || ['']).map((pattern, index) => (
                        <div className="pattern-row" key={`${selectedStep.id}-pattern-${index}`}>
                          <input value={pattern || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, patterns: (current.settings.patterns || ['']).map((item, itemIndex) => itemIndex === index ? event.target.value : item) } }))} placeholder="([A-Z][a-z]+)" aria-label={`Regex pattern ${index + 1}`} />
                          {index < (selectedStep.settings?.patterns || ['']).length - 1 && <span className="pattern-or">OR</span>}
                          <button className="delete-button" type="button" onClick={() => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, patterns: (current.settings.patterns || ['']).filter((_, itemIndex) => itemIndex !== index) } }))} disabled={(selectedStep.settings?.patterns || ['']).length <= 1} title="Remove pattern">×</button>
                        </div>
                      ))}
                    </div>
                    <button className="secondary-button" type="button" onClick={() => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, patterns: [...(current.settings.patterns || ['']), ''] } }))}>+ Add regex pattern</button>
                  </>
                ) : selectedStep.type === SMARTIE_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Use Google Gemini with prompts and store its response in a recipe variable. Prompts support {'{'}variableName{'}'}, {'{'}currentRow.columnName{'}'}, and {'{'}columnName{'}'}.</p>
                    <label>Gemini model<select value={selectedStep.settings?.model || 'gemini-2.5-flash'} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, model: event.target.value } }))}>
                      <option value="gemini-2.5-flash">gemini-2.5-flash</option>
                    </select></label>
                    <label>System prompt<textarea value={selectedStep.settings?.system_prompt || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, system_prompt: event.target.value } }))} placeholder="How Smartie should behave" /></label>
                    <label>User prompt<textarea value={selectedStep.settings?.user_prompt || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, user_prompt: event.target.value } }))} placeholder="Ask Smartie something using recipe variables if needed" /></label>
                    <label>Output variable<select value={selectedStep.settings?.output_variable || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, output_variable: event.target.value } }))}>
                      <option value="">Choose a variable</option>
                      {variables.map((variable) => <option key={variable.id} value={variable.name}>{variable.name}</option>)}
                    </select></label>
                    <label>Output schema<select value={selectedStep.settings?.output_type || 'text'} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, output_type: event.target.value } }))}>
                      <option value="text">Text</option><option value="number">Number</option><option value="json">JSON</option>
                    </select></label>
                    {selectedStep.settings?.output_type === 'json' && (
                      <>
                        <div className="section-label">JSON fields</div>
                        <div className="smartie-schema-list">
                          {(selectedStep.settings?.schema || []).map((field, index) => (
                            <div className="smartie-schema-row" key={`${selectedStep.id}-schema-${index}`}>
                              <input value={field.name || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, schema: (current.settings.schema || []).map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item) } }))} placeholder="Field name" />
                              <select value={field.type || 'string'} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, schema: (current.settings.schema || []).map((item, itemIndex) => itemIndex === index ? { ...item, type: event.target.value } : item) } }))}>
                                <option value="string">Text</option><option value="number">Number</option><option value="boolean">Boolean</option><option value="json">Object</option>
                              </select>
                              <label className="check-option"><input type="checkbox" checked={field.required !== false} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, schema: (current.settings.schema || []).map((item, itemIndex) => itemIndex === index ? { ...item, required: event.target.checked } : item) } }))} /> Required</label>
                              <button className="delete-button" type="button" onClick={() => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, schema: (current.settings.schema || []).filter((_, itemIndex) => itemIndex !== index) } }))} disabled={(selectedStep.settings?.schema || []).length <= 1} title="Remove JSON field">×</button>
                            </div>
                          ))}
                        </div>
                        <button className="secondary-button" type="button" onClick={() => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, schema: [...(current.settings.schema || []), { name: '', type: 'string', required: true }] } }))}>+ Add JSON field</button>
                      </>
                    )}
                  </>
                ) : selectedStep.type === CHECK_ELEMENT_EXISTS_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Check whether this selector exists, then choose the Exists or Not exists branch in Blocks to add nested steps.</p>
                    <label>Selector<input value={selectedStep.settings?.selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, selector: event.target.value } }))} placeholder="CSS selector or {{selectorVariable}}" /></label>
                    <button className="secondary-button picker-launch" disabled={!navigationRunCompleted} onClick={() => activatePicker(selectedStep.id, 'check_element_exists')}>Pick selector in TV</button>
                    <label>Timeout (seconds)<input type="number" min="0.1" max="3600" step="0.1" value={selectedStep.settings?.timeout_seconds ?? 5} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, timeout_seconds: event.target.value } }))} /></label>
                  </>
                ) : selectedStep.type === CHECK_TEXT_EXISTS_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Check for text or a recipe variable such as {'{{'}status{'}}'}, then use the Exists or Not exists branch in Blocks.</p>
                    <label>Text or variable<input value={selectedStep.settings?.text || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, text: event.target.value } }))} placeholder="Success or {{status}}" /></label>
                    <label>Wait up to (seconds)<input type="number" min="0.1" max="3600" step="0.1" value={selectedStep.settings?.timeout_seconds ?? 5} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, timeout_seconds: event.target.value } }))} /></label>
                    <label className="check-option"><input type="checkbox" checked={selectedStep.settings?.case_insensitive !== false} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, case_insensitive: event.target.checked } }))} /> Case insensitive</label>
                  </>
                ) : selectedStep.type === FOR_STEP_TYPE ? (
                  <>
                    {selectedStep.settings?.source === 'table' ? (
                    <>
                    <p className="detail-copy">Read each row or selected column from a local SQLite table, then run the steps in Loop body.</p>
                    <label>Current row<input value="currentRow" readOnly aria-label="Current row variable" /></label>
                    <p className="detail-copy">Use <code>{'{'}currentRow.columnName{'}'}</code> in nested steps. The current row can also be selected by Writer to update this row.</p>
                    <label>Read<select value={selectedStep.settings?.table_mode || 'row'} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, source: 'table', table_mode: event.target.value, iterator: event.target.value === 'row' ? 'row' : (current.settings?.column_name || 'value') } }))}>
                      <option value="row">each row</option><option value="column">one column value per row</option>
                    </select></label>
                    <label>Table<select value={selectedStep.settings?.table_name || ''} onChange={(event) => { updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, source: 'table', table_name: event.target.value } })); loadLoopTableSchema(event.target.value) }}>
                      <option value="">Choose a table</option>{writerTables.map((table) => <option key={table} value={table}>{table}</option>)}
                    </select></label>
                    {!writerTables.length && <label>Table name<input value={selectedStep.settings?.table_name || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, source: 'table', table_name: event.target.value } }))} placeholder="events" /></label>}
                    {selectedStep.settings?.table_mode === 'column' ? (
                      <>
                        <label>Column<select value={selectedStep.settings?.column_name || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, source: 'table', column_name: event.target.value, iterator: ['row', 'value', 'i'].includes(current.settings?.iterator) ? event.target.value : (current.settings?.iterator || event.target.value) } }))} disabled={loopSchemaLoading || !loopTableColumns.length}>
                          <option value="">Choose a column</option>{loopTableColumns.map((column) => <option key={column.name} value={column.name}>{column.name}</option>)}
                        </select></label>
                        <label>Value variable<input value={selectedStep.settings?.iterator || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, source: 'table', iterator: event.target.value } }))} placeholder="column_name" /></label>
                        <p className="detail-copy">Each row sets <code>{'{'}{selectedStep.settings?.iterator || 'column_name'}{'}'}</code> to that column's value.</p>
                      </>
                    ) : (
                      <>
                        <label>Row variable<input value={selectedStep.settings?.iterator || 'row'} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, source: 'table', iterator: event.target.value } }))} placeholder="row" /></label>
                        <p className="detail-copy">Each row is available as <code>{'{'}row.columnName{'}'}</code>, for example <code>{'{'}row.title{'}'}</code>.</p>
                      </>
                    )}
                    </>
                    ) : (
                    <>
                    <p className="detail-copy">Repeat the steps in Loop body for a fixed count or recipe variable.</p>
                    <label>Iterator<input value={`${selectedStep.settings?.iterator || 'i'}${Number(selectedStep.settings?.start_value || 0) !== 0 ? `=${selectedStep.settings.start_value}` : ''}`} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, source: 'range', iterator: event.target.value, start_value: 0 } }))} placeholder="i or i=1" /></label>
                    <label>Range<input value={selectedStep.settings?.range_value || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, source: 'range', range_value: event.target.value } }))} placeholder="{variable}+1, {variable}-1, or 10" /></label>
                    </>
                    )}
                    <p className="detail-copy">Click Loop body in Blocks to add steps that run on every iteration.</p>
                  </>
                ) : selectedStep.type === IF_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Compare values and place steps in the true or false branch from Blocks.</p>
                    <label>Left value<input value={selectedStep.settings?.left || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, left: event.target.value } }))} placeholder="{{variable}}" /></label>
                    <label>Operator<select value={selectedStep.settings?.operator || '='} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, operator: event.target.value } }))}>{['=', '<>', '<', '<=', '>', '>=', 'in'].map((operator) => <option key={operator} value={operator}>{operator}</option>)}</select></label>
                    <label>Right value<input value={selectedStep.settings?.right || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, right: event.target.value } }))} placeholder="Expected value or {{variable}}" /></label>
                  </>
                ) : selectedStep.type === USE_JS_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Paste the body of <code>function suiJSRunner(args) &#123; ... &#125;</code>. Use <code>args[0]</code>, <code>args[1]</code>, and so on. Inputs and the return variable are optional.</p>
                    <label>Function body<textarea value={selectedStep.settings?.code || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, code: event.target.value } }))} placeholder="return args[0];" /></label>
                    <div className="section-label">Input references (optional)</div>
                    {(selectedStep.settings?.inputs || []).map((inputName, index) => (
                      <div className="use-js-input-row" key={`${selectedStep.id}-input-${index}`}>
                        <span>args[{index}]</span>
                        <input value={inputName} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, inputs: (current.settings.inputs || []).map((item, itemIndex) => itemIndex === index ? event.target.value : item) } }))} placeholder="{variableName}" />
                        <button className="delete-button" type="button" onClick={() => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, inputs: (current.settings.inputs || []).filter((_, itemIndex) => itemIndex !== index) } }))}>×</button>
                      </div>
                    ))}
                    <button className="secondary-button" type="button" onClick={() => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, inputs: [...(current.settings.inputs || []), ''] } }))}>+ Input reference</button>
                    <label>Return variable (optional)<input value={selectedStep.settings?.output || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, output: event.target.value } }))} placeholder="{variableName}" /></label>
                    <label>Expected URL (optional)<input value={selectedStep.settings?.expected_url || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, expected_url: event.target.value } }))} placeholder="https://example.com/detail" /></label>
                    <label>Destination selector (optional)<input value={selectedStep.settings?.destination_selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, destination_selector: event.target.value } }))} placeholder="Selector that appears after Use JS" /></label>
                    <label>Wait timeout (seconds)<input type="number" min="0.1" max="120" step="0.1" value={selectedStep.settings?.wait_timeout_seconds ?? 15} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, wait_timeout_seconds: event.target.value } }))} /></label>
                  </>
                ) : selectedStep.type === MESSAGE_BOX_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Show a message in the browser. The run continues after OK is clicked.</p>
                    <label>Message<textarea value={selectedStep.settings?.message || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, message: event.target.value } }))} placeholder="Message to show" /></label>
                  </>
                ) : selectedStep.type === WRITER_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Define the columns to write. A missing table is created automatically; new columns are added to an existing table.</p>
                    <label>Table name<input value={selectedStep.settings?.table_name || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, table_name: event.target.value } }))} placeholder="events" /></label>
                    <label>Current row (optional)<select value={selectedStep.settings?.current_row || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, current_row: event.target.value } }))}>
                      <option value="">Append a new row</option>
                      <option value="currentRow">currentRow (from For each)</option>
                    </select></label>
                    <div className="writer-table-actions">
                      <select value="" onChange={(event) => { if (event.target.value) loadWriterSchema(event.target.value) }} aria-label="Load existing table schema">
                        <option value="">Load existing table schema</option>
                        {writerTables.map((table) => <option key={table} value={table}>{table}</option>)}
                      </select>
                      <button className="secondary-button" type="button" onClick={() => loadWriterSchema()} disabled={writerSchemaLoading || !selectedStep.settings?.table_name?.trim()}>{writerSchemaLoading ? 'Loading…' : 'Load schema'}</button>
                      <button className="secondary-button" type="button" onClick={loadWriterTables}>Refresh</button>
                    </div>
                    <label className="writer-duplicate-option"><input type="checkbox" checked={selectedStep.settings?.semantic_duplicate_removal === true} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, semantic_duplicate_removal: event.target.checked } }))} /> Semantic duplicate removal</label>
                    {writerSchemaError && <div className="detail-error">{writerSchemaError}</div>}
                    <div className="writer-columns">
                      <div className="writer-columns-heading"><span>Write</span><span>Column</span><span>Type</span><span>Value / expression</span><span /></div>
                      {writerColumns.map((column, index) => (
                        <div className="writer-column-row" key={`${selectedStep.id}-writer-column-${index}`}>
                          <input type="checkbox" checked={column.write !== false} onChange={(event) => updateWriterColumns((columns) => columns.map((item, itemIndex) => itemIndex === index ? { ...item, write: event.target.checked } : item))} aria-label={`Write ${column.name || `column ${index + 1}`}`} />
                          <input value={column.name || ''} onChange={(event) => updateWriterColumns((columns) => columns.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item))} placeholder="Column name" />
                          <select value={column.type || 'TEXT'} onChange={(event) => updateWriterColumns((columns) => columns.map((item, itemIndex) => itemIndex === index ? { ...item, type: event.target.value } : item))}>
                            {['TEXT', 'INTEGER', 'REAL', 'BOOLEAN', 'DATE', 'DATETIME', 'BLOB'].map((type) => <option key={type} value={type}>{type}</option>)}
                          </select>
                          <input value={column.value || ''} onChange={(event) => updateWriterColumns((columns) => columns.map((item, itemIndex) => itemIndex === index ? { ...item, value: event.target.value } : item))} placeholder={'{varOne + "Organizer: " + varTwo}'} />
                          <button className="delete-button" type="button" onClick={() => updateWriterColumns((columns) => columns.filter((_, itemIndex) => itemIndex !== index))} disabled={writerColumns.length <= 1} title="Remove column">×</button>
                        </div>
                      ))}
                    </div>
                    <button className="secondary-button" type="button" onClick={() => updateWriterColumns((columns) => [...columns, { name: '', type: 'TEXT', value: '', write: true }])}>+ Add column</button>
                  </>
                ) : [CALL_RECIPE_STEP_TYPE, INVOKE_RECIPE_STEP_TYPE].includes(selectedStep.type) ? (
                  <>
                    <p className="detail-copy">{selectedStep.type === INVOKE_RECIPE_STEP_TYPE ? 'Invoke a selected recipe in this position. Invokes run sequentially with the surrounding steps.' : 'Run another recipe from this project in the current browser session.'}</p>
                    <label>Recipe<select value={selectedStep.settings?.recipe_id ? `id:${selectedStep.settings.recipe_id}` : (selectedStep.settings?.recipe_name ? `name:${selectedStep.settings.recipe_name}` : '')} onChange={(event) => {
                      const target = (selectedStep.type === INVOKE_RECIPE_STEP_TYPE ? invokeRecipes : callableRecipes).find((recipe) => (recipe.id ? `id:${recipe.id}` : `name:${recipe.name}`) === event.target.value)
                      updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, recipe_id: target?.id || '', recipe_name: target?.name || '' } }))
                    }}>
                      <option value="">Choose a recipe</option>{(selectedStep.type === INVOKE_RECIPE_STEP_TYPE ? invokeRecipes : callableRecipes).map((recipe) => <option key={recipe.localKey} value={recipe.id ? `id:${recipe.id}` : `name:${recipe.name}`}>{recipe.name}</option>)}
                    </select></label>
                  </>
                ) : (
                  <>
                    <p className="detail-copy">Open this URL when the recipe runs.</p>
                    <label>URL<input value={selectedStep.settings?.url || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, url: event.target.value } }))} placeholder="https://example.com" /></label>
                  </>
                )}
                <button className="back-to-view" type="button" onClick={() => setRightMode('viewer')}>BACK TO VIEW</button>
              </div>
            )}
            {rightMode === 'config' && activeRecipe && (
              <div className="recipe-config-panel">
                <div className="config-header">
                  <div>
                    <div className="section-label">Configuration</div>
                    <h3>{activeRecipe.name || 'Untitled recipe'}</h3>
                  </div>
                  <button className="back-to-view config-back" type="button" onClick={() => setRightMode('viewer')}>BACK TO VIEW</button>
                </div>
                <div className="config-section">
                  <div className="section-label">Recipe type</div>
                  <label><select value={activeRecipe.definition?.recipe_type || 'standard'} onChange={(event) => updateActiveRecipe((recipe) => ({ ...recipe, definition: { ...recipe.definition, recipe_type: event.target.value } }))}>
                    <option value="standard">Standard</option>
                    <option value="orchest">Orchest</option>
                  </select></label>
                  <p className="detail-copy">Orchest recipes can run other recipes from this project.</p>
                </div>
                <div className="config-section">
                  <div className="section-label">Variables</div>
                  <p className="detail-copy">Create recipe-scoped values and use them with Assign steps.</p>
                  <div className="variable-create-row">
                    <input value={variableName} onChange={(event) => setVariableName(event.target.value)} placeholder="Variable name" aria-label="Variable name" />
                    <input value={variableValue} onChange={(event) => setVariableValue(event.target.value)} placeholder="Initial value" aria-label="Initial value" />
                    <button className="primary-button" type="button" onClick={createVariable} disabled={!variableName.trim()}>Add variable</button>
                  </div>
                  {variables.length ? (
                    <div className="variable-list">
                      {variables.map((variable) => (
                        <div className="variable-row" key={variable.id}>
                          <input value={variable.name} readOnly aria-label={`${variable.name} variable name`} />
                          <input value={variable.value} onChange={(event) => updateVariable(variable.id, { value: event.target.value })} aria-label={`${variable.name} variable value`} />
                          <button className="delete-button" type="button" title={`Remove ${variable.name}`} onClick={() => removeVariable(variable.id)}>×</button>
                        </div>
                      ))}
                    </div>
                  ) : <div className="empty-sidebar">No variables yet</div>}
                </div>
                <div className="config-section">
                  <div className="section-label">Mode</div>
                  <p className="detail-copy">Choose how Chrome opens when this recipe runs.</p>
                  <div className="mode-options">
                    {['headful', 'headless'].map((mode) => (
                      <label className="mode-option" key={mode}>
                        <input type="radio" name={`recipe-mode-${activeRecipe.localKey}`} checked={(activeRecipe.definition.mode || 'headful') === mode} onChange={() => updateActiveRecipe((recipe) => ({ ...recipe, definition: { ...recipe.definition, mode } }))} />
                        <span>{mode[0].toUpperCase() + mode.slice(1)}</span>
                      </label>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
      {pendingDeleteRecipe && (
        <div className="confirm-overlay" role="presentation" onClick={() => setPendingDeleteRecipe(null)}>
          <div className="confirm-box" role="dialog" aria-modal="true" aria-labelledby="delete-recipe-title" onClick={(event) => event.stopPropagation()}>
            <div className="section-label">Delete recipe</div>
            <h3 id="delete-recipe-title">Delete “{pendingDeleteRecipe.name || 'Untitled recipe'}”?</h3>
            <p>This cannot be undone.</p>
            <div className="confirm-actions">
              <button className="secondary-button" type="button" onClick={() => setPendingDeleteRecipe(null)}>Cancel</button>
              <button className="primary-button danger-button" type="button" onClick={async () => { const recipe = pendingDeleteRecipe; setPendingDeleteRecipe(null); await deleteRecipe(recipe) }}>Delete recipe</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

function SelectorPicker({ execution, pickerTrial, pickerAction, pickerValue, variables, pickerVariable, newVariableName, onChooseSelector, onChooseAction, onChangeValue, onChangeVariable, onChangeNewVariable, onCreateVariable, onTrySelector, onRepick, onConfirm, onCancel }) {
  const element = execution.pickerElement
  const listedCandidates = (Array.isArray(element?.selectorCandidates) ? element.selectorCandidates : [])
    .map((candidate) => typeof candidate === 'string' ? { selector: candidate, kind: 'Selector', unique: null, matches: null } : candidate)
    .filter((candidate) => String(candidate?.selector || '').trim())
  const candidates = listedCandidates.length || !String(element?.selector || '').trim()
    ? listedCandidates
    : [{ selector: String(element.selector).trim(), kind: 'Selector', unique: null, matches: null }]
  const selectedSelector = String(element?.selectedSelector || pickerTrial.selector || element?.selector || '').trim()
  const trialRunning = pickerTrial.status === 'running'
  const trialFinished = pickerTrial.status === 'completed' || pickerTrial.status === 'failed'
  const checkElementAction = pickerAction === 'check_element_exists'
  const actionLabel = checkElementAction ? 'Check element exists' : pickerAction === 'fill' ? 'Fill' : pickerAction === 'scrape' ? 'Scrape' : 'Click'
  return (
    <div className={element ? 'selector-picker-panel has-element' : 'selector-picker-panel'} onClick={(event) => event.stopPropagation()}>
      <div className="selector-picker-toolbar">
        <span className="section-label">Pick an element</span>
        {!checkElementAction && <div className="selector-picker-actions">
          <button className={pickerAction === 'click' ? 'selector-action active' : 'selector-action'} type="button" onClick={() => onChooseAction('click')}>Click</button>
          <button className={pickerAction === 'fill' ? 'selector-action active' : 'selector-action'} type="button" onClick={() => onChooseAction('fill')}>Fill</button>
          <button className={pickerAction === 'scrape' ? 'selector-action active' : 'selector-action'} type="button" onClick={() => onChooseAction('scrape')}>Scrape</button>
        </div>}
      </div>
      {!element ? (
        <div className="selector-picker-empty" aria-hidden="true" />
      ) : (
        <>
          <div className="selector-picker-element">
            <strong>{element.metadata?.tag || 'Element'}{element.metadata?.text ? ` · ${element.metadata.text}` : ''}</strong>
            <span>{element.metadata?.ariaLabel || element.metadata?.role || 'Choose a selector to try'}</span>
          </div>
          {element.error && <div className="selector-picker-error">{element.error}</div>}
          {pickerAction === 'fill' && <label className="picker-value-field">Value<input value={pickerValue} onChange={(event) => onChangeValue(event.target.value)} placeholder="Text to fill" /></label>}
          {pickerAction === 'scrape' && (
            <div className="scrape-variable-fields">
              <label>Store scraped text in
                <select value={pickerVariable} onChange={(event) => onChangeVariable(event.target.value)}>
                  <option value="">Choose a variable</option>
                  {variables.map((variable) => <option key={variable.id} value={variable.name}>{variable.name}</option>)}
                </select>
              </label>
              <div className="inline-variable-create">
                <input value={newVariableName} onChange={(event) => onChangeNewVariable(event.target.value)} placeholder="New variable name" aria-label="New scrape variable name" />
                <button className="secondary-button" type="button" disabled={!newVariableName.trim()} onClick={onCreateVariable}>Create variable</button>
              </div>
            </div>
          )}
          <div className="selector-picker-list">
            {candidates.map((candidate, index) => {
              const selector = String(candidate.selector).trim()
              const selected = selector === selectedSelector
              const uniqueness = candidate.unique === true ? 'unique' : candidate.matches !== null && candidate.matches !== undefined ? `${candidate.matches} matches` : ''
              return (
                <button className={selected ? 'selector-candidate selected' : 'selector-candidate'} type="button" key={`${selector}-${index}`} onClick={() => onChooseSelector(selector)}>
                  <span className="selector-candidate-meta"><strong>{candidate.kind || 'Selector'}</strong>{uniqueness && <small>{uniqueness}</small>}</span>
                  <code>{selector}</code>
                </button>
              )
            })}
          </div>
          {pickerTrial.error && <div className="selector-picker-error">{pickerTrial.error}</div>}
          {pickerTrial.status === 'completed' && <div className="selector-picker-success">{checkElementAction ? `${pickerTrial.value || 'Selector checked'}.` : pickerAction === 'fill' ? 'Fill completed with this selector.' : pickerAction === 'scrape' ? `Scraped: ${pickerTrial.value || '(empty)'}` : 'Click completed with this selector.'}</div>}
          {trialFinished && <div className="selector-picker-confirm">Confirm this {actionLabel} step with the selected selector.</div>}
          <div className="selector-picker-footer">
            <button className="primary-button" type="button" disabled={!selectedSelector || trialRunning || (pickerAction === 'fill' && !pickerValue) || (pickerAction === 'scrape' && !pickerVariable)} onClick={onTrySelector}>{trialRunning ? 'Trying…' : checkElementAction ? 'Check selector' : pickerAction === 'fill' ? 'Try Fill' : pickerAction === 'scrape' ? 'Try Scrape' : 'Try Click'}</button>
            <button className="primary-button" type="button" disabled={!selectedSelector || (pickerAction === 'fill' && !pickerValue) || (pickerAction === 'scrape' && !pickerVariable)} onClick={onConfirm}>Confirm</button>
            <button className="secondary-button" type="button" onClick={onRepick}>Repick</button>
            <button className="secondary-button" type="button" onClick={onCancel}>Cancel</button>
          </div>
        </>
      )}
      {!element && <div className="selector-picker-footer"><button className="secondary-button" type="button" onClick={onCancel}>Cancel</button></div>}
    </div>
  )
}

function ExecutionViewer({ execution, steps, onInput, pickerActive, pickerEnabled, onPickerToggle }) {
  const hoverTimerRef = useRef(null)
  const pendingPointRef = useRef(null)
  useEffect(() => () => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current)
  }, [])
  const statusLabel = execution.status === 'idle' ? '' : execution.status === 'completed' ? 'Completed' : execution.status === 'failed' ? 'Failed' : 'Running'
  const stepStates = buildStepExecutionStates(execution)
  const currentStepId = latestExecutionStepId(execution)
  const currentStep = findStepInTree(steps, currentStepId) || (execution.stepId === currentStepId && execution.stepType ? { type: execution.stepType } : null)
  const currentStepState = stepStates[currentStepId]
  const currentStepStatus = currentStepState?.status || (currentStep ? execution.status : '')
  const currentStepError = currentStepState?.error || (currentStepStatus === 'failed' ? execution.error : '')
  const currentStepDetails = executionStepDetails(currentStep)
  const handleKeyDown = (event) => {
    if (!onInput || !(execution.navigationRunId || execution.runId)) return
    if (event.key.length === 1) {
      event.preventDefault()
      onInput({ type: 'text', text: event.key })
    } else if (['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) {
      event.preventDefault()
      onInput({ type: 'key', key: event.key })
    }
  }
  const imagePoint = (event, image) => {
    if (!onInput || !(execution.navigationRunId || execution.runId)) return
    if (!image?.naturalWidth || !image?.naturalHeight) return null
    const rect = image.getBoundingClientRect()
    const sourceWidth = image.naturalWidth
    const sourceHeight = image.naturalHeight
    const scale = Math.min(rect.width / sourceWidth, rect.height / sourceHeight)
    const renderedWidth = sourceWidth * scale
    const renderedHeight = sourceHeight * scale
    const x = (event.clientX - rect.left - (rect.width - renderedWidth) / 2) / scale
    const y = (event.clientY - rect.top - (rect.height - renderedHeight) / 2) / scale
    return x >= 0 && y >= 0 && x <= sourceWidth && y <= sourceHeight
      ? { x, y, source_width: sourceWidth, source_height: sourceHeight }
      : null
  }
  const handleImageClick = (event, image = event.currentTarget) => {
    const point = imagePoint(event, image)
    if (point) onInput({ type: pickerActive ? 'inspect' : 'click', ...point })
  }
  const handleViewerMouseMove = (event) => {
    if (!pickerActive) return
    const image = event.target.closest?.('img')
    const point = image ? imagePoint(event, image) : null
    if (!point) return
    pendingPointRef.current = point
    if (hoverTimerRef.current) return
    hoverTimerRef.current = setTimeout(() => {
      hoverTimerRef.current = null
      const nextPoint = pendingPointRef.current
      pendingPointRef.current = null
      if (nextPoint) onInput({ type: 'inspect_move', ...nextPoint })
    }, 55)
  }
  const handleViewerMouseLeave = () => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current)
    hoverTimerRef.current = null
    pendingPointRef.current = null
    if (pickerActive) onInput({ type: 'clear_picker_overlay' })
  }
  const handleViewerClick = (event) => {
    event.currentTarget.focus()
    const image = event.target.closest?.('img')
    if (image) handleImageClick(event, image)
  }
  return (
    <div
      className={execution.frame ? 'execution-viewer has-frame' : 'execution-viewer'}
      tabIndex="0"
      onClick={handleViewerClick}
      onMouseMove={handleViewerMouseMove}
      onMouseLeave={handleViewerMouseLeave}
      onKeyDown={handleKeyDown}
    >
      {pickerEnabled && !pickerActive && (
        <button className={pickerActive ? 'picker-toggle active' : 'picker-toggle'} onClick={(event) => { event.stopPropagation(); onPickerToggle() }}>
          {pickerActive ? 'Picker on' : 'Picker'}
        </button>
      )}
      {execution.frame ? <img src={execution.frame} alt="Live browser execution" /> : <div className="viewer-mark">SUI</div>}
      {currentStep && currentStepStatus && (
        <div className={`viewer-progress ${currentStepStatus}`} role="status">
          <span>{currentStepStatus === 'failed' ? 'Failed step' : currentStepStatus === 'running' ? 'Current step' : 'Last step'}</span>
          <strong>{stepLabel(currentStep)}</strong>
          {currentStepDetails && <small>{currentStepDetails}</small>}
          {currentStepStatus === 'failed' && currentStepError && <em>{currentStepError}</em>}
        </div>
      )}
      {statusLabel && <span className={`viewer-status ${execution.status}`}>{statusLabel}</span>}
    </div>
  )
}

function DataView() {
  const [mode, setMode] = useState('view')
  const [craftMode, setCraftMode] = useState('schema')
  const [tables, setTables] = useState([])
  const [selectedTable, setSelectedTable] = useState('')
  const [tableData, setTableData] = useState(null)
  const [craftTable, setCraftTable] = useState('')
  const [craftColumns, setCraftColumns] = useState([{ name: 'value', type: 'TEXT' }])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [addingRow, setAddingRow] = useState(false)
  const [newRow, setNewRow] = useState({})
  const [editingCell, setEditingCell] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const loadTables = async (keepSelection = true) => {
    setLoading(true)
    try {
      const result = await api('/data/tables')
      const nextTables = result.tables || []
      setTables(nextTables)
      if (!keepSelection || !nextTables.includes(selectedTable)) {
        setSelectedTable(nextTables[0] || '')
      }
      setError('')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadTables(false) }, [])

  const loadTableData = async (tableName) => {
    const name = String(tableName || '').trim()
    if (!name) { setTableData(null); return }
    setLoading(true)
    try {
      const result = await api(`/data/table?table=${encodeURIComponent(name)}&limit=500`)
      setTableData(result)
      setError('')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const tableName = mode === 'view' ? selectedTable : craftMode === 'data' ? (craftTable || selectedTable) : ''
    if (!tableName) {
      setTableData(null)
      return undefined
    }
    let cancelled = false
    setLoading(true)
    api(`/data/table?table=${encodeURIComponent(tableName)}&limit=500`)
      .then((result) => { if (!cancelled) { setTableData(result); setError('') } })
      .catch((reason) => { if (!cancelled) setError(reason.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [mode, craftMode, selectedTable, craftTable])

  const loadCraftSchema = async (tableName) => {
    const name = String(tableName || '').trim()
    if (!name) return
    setLoading(true)
    try {
      const result = await api(`/writer/schema?table=${encodeURIComponent(name)}`)
      setCraftTable(result.table || name)
      setCraftColumns((result.columns || []).map((column) => ({ name: column.name, original_name: column.name, type: column.type || 'TEXT' })))
      setSelectedTable(name)
      setError('')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setLoading(false)
    }
  }

  const beginNewTable = () => {
    setMode('craft')
    setCraftMode('schema')
    setCraftTable('')
    setCraftColumns([{ name: 'value', type: 'TEXT' }])
    setTableData(null)
    setAddingRow(false)
    setNotice('')
    setError('')
  }

  const saveCraftTable = async () => {
    if (!craftTable.trim()) { setError('Table name is required'); return }
    setSaving(true)
    setError('')
    try {
      const result = await api('/data/table', {
        method: 'POST',
        body: JSON.stringify({ table: craftTable.trim(), columns: craftColumns }),
      })
      setCraftTable(result.table)
      setCraftColumns((result.columns || []).map((column) => ({ name: column.name, original_name: column.name, type: column.type || 'TEXT' })))
      setNotice(result.created ? 'Table created' : 'Table schema updated')
      await loadTables(false)
      setSelectedTable(result.table)
      setCraftMode('schema')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setSaving(false)
    }
  }

  const deleteCraftTable = async () => {
    const name = craftTable.trim()
    if (!name || !window.confirm(`Delete table "${name}" and all its rows?`)) return
    setSaving(true)
    setError('')
    try {
      await api('/data/table', { method: 'DELETE', body: JSON.stringify({ table: name }) })
      setCraftTable('')
      setCraftColumns([{ name: 'value', type: 'TEXT' }])
      setTableData(null)
      setSelectedTable('')
      setCraftMode('schema')
      setNotice('Table deleted')
      await loadTables(false)
    } catch (reason) {
      setError(reason.message)
    } finally {
      setSaving(false)
    }
  }

  const selectTable = (table) => {
    setSelectedTable(table)
    setNotice('')
    setAddingRow(false)
    setEditingCell(null)
    if (mode === 'craft') loadCraftSchema(table)
  }

  const startAddingRow = () => {
    if (!tableData) return
    setNewRow(Object.fromEntries(tableData.columns.map((column) => [column.name, ''])))
    setAddingRow(true)
    setEditingCell(null)
    setError('')
  }

  const saveNewRow = async () => {
    if (!tableData) return
    setSaving(true)
    setError('')
    try {
      const result = await api('/data/table/row', {
        method: 'POST',
        body: JSON.stringify({ table: tableData.table, values: newRow }),
      })
      setTableData(result)
      setNewRow({})
      setAddingRow(false)
      setNotice('Row added')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setSaving(false)
    }
  }

  const saveDataCell = async (row, columnName, nextValue) => {
    if (!tableData || !row?.__sui_rowid) return
    setSaving(true)
    setError('')
    try {
      const result = await api('/data/table/row', {
        method: 'PUT',
        body: JSON.stringify({ table: tableData.table, row_id: row.__sui_rowid, column: columnName, value: nextValue }),
      })
      setTableData(result)
      setNotice('Cell updated')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setEditingCell(null)
      setSaving(false)
    }
  }

  const deleteDataRow = async (row) => {
    if (!tableData || !row?.__sui_rowid || !window.confirm('Delete this row?')) return
    setSaving(true)
    setError('')
    try {
      const result = await api('/data/table/row', {
        method: 'DELETE',
        body: JSON.stringify({ table: tableData.table, row_id: row.__sui_rowid }),
      })
      setTableData(result)
      setNotice('Row deleted')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setSaving(false)
    }
  }

  useEffect(() => {
    if (mode === 'craft' && selectedTable) loadCraftSchema(selectedTable)
  }, [mode])

  const columnTypes = ['TEXT', 'INTEGER', 'REAL', 'BOOLEAN', 'DATE', 'DATETIME', 'BLOB']

  return (
    <section className="page-section viewer-page data-page">
      <div className="section-heading viewer-heading">
        <div><div className="eyebrow">DATA</div><h1>{mode === 'view' ? 'View' : 'Craft'}</h1><p>{mode === 'view' ? 'Inspect the tables created by your recipes.' : 'Create and shape data-tables.'}</p></div>
        <div className="data-mode-actions">
          <button className={mode === 'view' ? 'data-mode-button active' : 'data-mode-button'} type="button" onClick={() => setMode('view')}>View</button>
          <button className={mode === 'craft' ? 'data-mode-button active' : 'data-mode-button'} type="button" onClick={() => setMode('craft')}>Craft</button>
        </div>
      </div>
      {error && <div className="viewer-error-banner">{error}</div>}
      {notice && <Toast message={notice} onClose={() => setNotice('')} />}
      <div className="viewer-layout">
        <aside className="viewer-table-list" aria-label="SQLite tables">
          <div className="viewer-list-header"><div className="viewer-list-label">TABLES</div><button className="mini-action" type="button" onClick={() => loadTables(false)}>↻</button></div>
          {loading && !tables.length ? <div className="empty-sidebar">Loading…</div> : tables.length ? tables.map((table) => (
            <button key={table} className={selectedTable === table ? 'viewer-table-button active' : 'viewer-table-button'} type="button" onClick={() => selectTable(table)}>{table}</button>
          )) : <div className="empty-sidebar">No tables yet</div>}
          {mode === 'craft' && <button className="new-table-button" type="button" onClick={beginNewTable}>+ New table</button>}
        </aside>
        <div className="viewer-table-content">
          {mode === 'craft' ? (
            <div className="craft-panel">
              <div className="viewer-table-title craft-title"><div><div className="eyebrow">{craftMode === 'schema' ? 'TABLE DESIGN' : 'TABLE DATA'}</div><h2>{craftTable || 'New table'}</h2></div><div className="craft-header-actions"><div className="craft-edit-mode"><button type="button" className={craftMode === 'schema' ? 'craft-edit-button active' : 'craft-edit-button'} onClick={() => setCraftMode('schema')}>Schema Edit</button><button type="button" className={craftMode === 'data' ? 'craft-edit-button active' : 'craft-edit-button'} onClick={() => setCraftMode('data')}>Data Edit</button></div><button className="primary-button" type="button" onClick={saveCraftTable} disabled={saving}>{saving ? 'Saving…' : 'Save table'}</button>{craftTable && <button className="danger-button" type="button" onClick={deleteCraftTable} disabled={saving}>Delete table</button>}</div></div>
              {craftMode === 'schema' ? (
                <>
                  <label>Table name<input value={craftTable} onChange={(event) => setCraftTable(event.target.value)} placeholder="events" /></label>
                  <div className="craft-columns-heading"><span>Column</span><span>Type</span><span /></div>
                  <div className="craft-columns">
                    {craftColumns.map((column, index) => <div className="craft-column-row" key={`craft-column-${index}`}><input value={column.name} onChange={(event) => setCraftColumns((columns) => columns.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item))} placeholder="Column name" /><select value={column.type} onChange={(event) => setCraftColumns((columns) => columns.map((item, itemIndex) => itemIndex === index ? { ...item, type: event.target.value } : item))}>{columnTypes.map((type) => <option key={type} value={type}>{type}</option>)}</select><button className="delete-button" type="button" disabled={craftColumns.length <= 1} onClick={() => setCraftColumns((columns) => columns.filter((_, itemIndex) => itemIndex !== index))}>×</button></div>)}
                  </div>
                  <button className="secondary-button" type="button" onClick={() => setCraftColumns((columns) => [...columns, { name: '', type: 'TEXT' }])}>+ Add column</button>
                  <p className="detail-copy">Choose a table from the left to edit its current schema, or start a new table.</p>
                </>
              ) : !tableData ? <div className="viewer-empty table-empty"><p>Save the table schema first, then add and edit rows here.</p></div> : (
                <div className="craft-data-editor">
                  <div className="craft-data-toolbar"><span>Double-click a cell to edit it.</span><button className="secondary-button" type="button" onClick={startAddingRow} disabled={addingRow || saving}>+ Add row</button></div>
                  <div className="viewer-data-wrap"><table className="viewer-data-table craft-data-table"><thead><tr><th className="row-number-heading">#</th>{tableData.columns.map((column) => <th key={column.name}>{column.name}<small>{column.type}</small></th>)}<th className="craft-row-action-heading">Action</th></tr></thead><tbody>
                    {tableData.rows.map((row, rowIndex) => <tr key={`${tableData.table}-${row.__sui_rowid}`}><td className="row-number">{rowIndex + 1}</td>{tableData.columns.map((column) => { const value = row[column.name]; const display = value && typeof value === 'object' ? value.value : value; const cellKey = `${row.__sui_rowid}:${column.name}`; return <td key={column.name} title="Double-click to edit" onDoubleClick={() => { setEditingCell(cellKey); setError('') }}>{editingCell === cellKey ? <input className="craft-cell-input" autoFocus defaultValue={display == null ? '' : String(display)} onBlur={(event) => saveDataCell(row, column.name, event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur(); if (event.key === 'Escape') setEditingCell(null) }} /> : display == null ? '—' : String(display)}</td> })}<td className="craft-row-action"><button type="button" className="craft-row-delete" onClick={() => deleteDataRow(row)} disabled={saving}>Delete</button></td></tr>)}
                    {addingRow && <tr className="craft-new-row"><td className="row-number">—</td>{tableData.columns.map((column) => <td key={column.name}><input value={newRow[column.name] ?? ''} onChange={(event) => setNewRow((values) => ({ ...values, [column.name]: event.target.value }))} placeholder={column.name} /></td>)}<td className="craft-row-action">—</td></tr>}
                  </tbody></table></div>
                  {addingRow && <div className="craft-row-actions"><button className="primary-button" type="button" onClick={saveNewRow} disabled={saving}>{saving ? 'Saving…' : 'Save row'}</button><button className="secondary-button" type="button" onClick={() => setAddingRow(false)} disabled={saving}>Cancel</button></div>}
                  {!tableData.rows.length && !addingRow && <div className="viewer-empty table-empty"><p>This table has no rows yet. Add one to begin.</p></div>}
                </div>
              )}
            </div>
          ) : !tableData ? <div className="viewer-empty"><span className="viewer-empty-mark">SUI</span><p>Select a table to view its contents.</p></div> : (
            <>
              <div className="viewer-table-title"><div><div className="eyebrow">TABLE</div><h2>{tableData.table}</h2></div><span>{tableData.rows.length} row{tableData.rows.length === 1 ? '' : 's'}</span></div>
              <div className="viewer-schema">{tableData.columns.map((column) => <span key={column.name}><strong>{column.name}</strong><small>{column.type}</small></span>)}</div>
              <div className="viewer-data-wrap">
                {tableData.rows.length ? <table className="viewer-data-table"><thead><tr><th className="row-number-heading">#</th>{tableData.columns.map((column) => <th key={column.name}>{column.name}</th>)}</tr></thead><tbody>{tableData.rows.map((row, index) => <tr key={`${tableData.table}-${index}`}><td className="row-number">{index + 1}</td>{tableData.columns.map((column) => { const value = row[column.name]; const display = value && typeof value === 'object' ? value.value : value; return <td key={column.name} title={display == null ? '' : String(display)}>{display == null ? '—' : String(display)}</td> })}</tr>)}</tbody></table> : <div className="viewer-empty table-empty"><p>This table has no rows.</p></div>}
              </div>
            </>
          )}
        </div>
      </div>
    </section>
  )
}

function ProjectsView({ projects, projectDetail, newProject, openProject, editProject, runRecipe, clearDetail }) {
  if (projectDetail) {
    return (
      <section className="page-section">
        <button className="back-button" onClick={clearDetail}>← Projects</button>
        <div className="section-heading"><div><span className="eyebrow">Project</span><h1>{projectDetail.name}</h1></div><button className="primary-button" onClick={() => editProject(projectDetail)}>Edit project</button></div>
        <div className="recipe-list">
          {projectDetail.recipes.map((recipe) => <RecipeCard key={recipe.id} recipe={recipe} runRecipe={runRecipe} />)}
          {!projectDetail.recipes.length && <div className="empty-state">No recipes yet. Open Edit project to add one.</div>}
        </div>
      </section>
    )
  }
  return (
    <section className="page-section">
      <div className="section-heading"><div><span className="eyebrow">Workspace</span><h1>Projects</h1></div><button className="primary-button" onClick={newProject}>New project</button></div>
      <div className="project-grid">
        {projects.map((project) => <button className="project-card" key={project.id} onClick={() => openProject(project)}><span className="card-kicker">Project</span><strong>{project.name}</strong><span>{project.recipes?.length || 0} recipe{project.recipes?.length === 1 ? '' : 's'}</span></button>)}
        {!projects.length && <div className="empty-state">No saved projects yet. Create one in Studio.</div>}
      </div>
    </section>
  )
}

function RecipeCard({ recipe, runRecipe }) {
  return <article className="recipe-card"><div><span className="card-kicker">Recipe</span><h2>{recipe.name}</h2><p>{recipe.definition?.steps?.length || 0} step{recipe.definition?.steps?.length === 1 ? '' : 's'}</p></div><button className="secondary-button" onClick={() => runRecipe(recipe)}>Run recipe</button></article>
}

function JobsView({ jobs, jobDetail, jobLive, openJob, stopJob, clearDetail }) {
  if (jobDetail) {
    const liveLogs = jobLive?.jobId === jobDetail.id ? jobLive.logs : []
    const jobStatus = jobLive?.jobId === jobDetail.id ? jobLive.status : jobDetail.status
    return (
      <section className="page-section">
        <button className="back-button" onClick={clearDetail}>← Jobs</button>
        <div className="section-heading"><div><span className="eyebrow">Execution</span><h1>{jobDetail.project_name} / {jobDetail.recipe_name}</h1></div><div className="job-heading-actions"><StatusBadge status={jobStatus} />{['queued', 'running'].includes(jobStatus) && <button className="stop-run-button" type="button" onClick={() => stopJob(jobDetail.id)}>Stop run</button>}</div></div>
        <div className="job-meta"><span>Started {formatTime(jobDetail.started_at)}</span><span>Ended {formatTime(jobDetail.ended_at)}</span></div>
        {(jobLive?.error || jobDetail.error) && <div className="error-banner">{jobLive?.error || jobDetail.error}</div>}
        {jobLive?.frame && <div className="job-live-viewer"><img src={jobLive.frame} alt="Live full recipe execution" /></div>}
        <div className="log-panel"><div className="section-label">Logs</div>{liveLogs.length ? liveLogs.map((log, index) => <div className={`log-line ${log.level}`} key={`${jobDetail.id}-live-${index}`}><time>Live</time><span>{friendlyExecutionLog(log.message, null, [])}</span></div>) : jobDetail.logs?.length ? jobDetail.logs.map((log) => <div className={`log-line ${log.level}`} key={log.id}><time>{formatTime(log.created_at)}</time><span>{friendlyExecutionLog(log.message, null, [])}</span></div>) : <div className="empty-state">No logs yet.</div>}</div>
      </section>
    )
  }
  return <section className="page-section"><div className="section-heading"><div><span className="eyebrow">History</span><h1>Jobs</h1></div></div><div className="job-table"><div className="job-table-header"><span>Project / recipe</span><span>Status</span><span>Started</span><span>Ended</span><span /></div>{jobs.map((job) => <div className="job-row" key={job.id} role="button" tabIndex="0" onClick={() => openJob(job)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') openJob(job) }}><span><strong>{job.project_name}</strong><small>{job.recipe_name}</small></span><StatusBadge status={job.status} /><span>{formatTime(job.started_at)}</span><span>{formatTime(job.ended_at)}</span>{['queued', 'running'].includes(job.status) ? <button className="job-stop-button" type="button" onClick={(event) => { event.stopPropagation(); stopJob(job.id) }}>Stop</button> : <span />}</div>)}{!jobs.length && <div className="empty-state">No executions yet. Run a recipe from Projects or Studio.</div>}</div></section>
}

function StatusBadge({ status }) {
  return <span className={`status-badge ${status}`}>{status}</span>
}

export default App
