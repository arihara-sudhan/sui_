import React, { useEffect, useMemo, useRef, useState } from 'react'

const STEP_TYPE = 'navigate_to_website'
const CLICK_STEP_TYPE = 'click'
const FILL_STEP_TYPE = 'fill'
const API_ROOT = 'http://127.0.0.1:8000/api'

const makeKey = (prefix) => `${prefix}-${crypto.randomUUID()}`
const stepLabel = (step) => (step.type === CLICK_STEP_TYPE ? 'Click' : step.type === FILL_STEP_TYPE ? 'Fill' : 'Navigate to website')

const blankDefinition = () => ({
  schema_version: 1,
  steps: [],
  connections: [],
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

const hydrateProject = (project) => ({
  projectId: project.id,
  projectName: project.name,
  recipes: (project.recipes || []).map((recipe) => ({
    localKey: recipe.id || makeKey('recipe'),
    id: recipe.id,
    name: recipe.name,
    definition: recipe.definition || blankDefinition(),
  })),
})

const formatTime = (value) => (value ? new Date(value).toLocaleString() : '—')

function App() {
  const [view, setView] = useState('studio')
  const [projects, setProjects] = useState([])
  const [jobs, setJobs] = useState([])
  const [studio, setStudio] = useState(blankStudio)
  const [activeRecipeKey, setActiveRecipeKey] = useState(null)
  const [projectDetail, setProjectDetail] = useState(null)
  const [jobDetail, setJobDetail] = useState(null)
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
    if (view !== 'jobs') return undefined
    const timer = window.setInterval(() => loadJobs().catch(() => {}), 2000)
    return () => window.clearInterval(timer)
  }, [view])

  const changeView = (nextView) => {
    setNotice('')
    setError('')
    setView(nextView)
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
    setActiveRecipeKey(project.recipes?.[0]?.id || null)
    setSelectedStepId(null)
    setNotice('')
    setError('')
    setView('studio')
  }

  const newProject = () => {
    const draft = blankStudio()
    setStudio(draft)
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
        steps: recipe.definition.steps.map((step) => (step.id === stepId ? updater(step) : step)),
      },
    }))
  }

  const deleteStep = (stepId) => {
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: { ...recipe.definition, steps: recipe.definition.steps.filter((step) => step.id !== stepId) },
    }))
    setSelectedStepId(null)
  }

  const addNavigationStep = (position = { x: 48, y: 48 }) => {
    const step = {
      id: makeKey('step'),
      type: STEP_TYPE,
      settings: { url: '' },
      connections: { next: [] },
      position,
    }
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: { ...recipe.definition, steps: [...recipe.definition.steps, step] },
    }))
    setSelectedStepId(step.id)
  }

  const addClickStep = (position = { x: 48, y: 48 }, selector = '') => {
    const step = {
      id: makeKey('step'),
      type: CLICK_STEP_TYPE,
      settings: { selector },
      connections: { next: [] },
      position,
    }
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: { ...recipe.definition, steps: [...recipe.definition.steps, step] },
    }))
    setSelectedStepId(step.id)
    return step.id
  }

  const addFillStep = (position = { x: 48, y: 48 }, selector = '', value = '') => {
    const step = {
      id: makeKey('step'),
      type: FILL_STEP_TYPE,
      settings: { selector, value },
      connections: { next: [] },
      position,
    }
    updateActiveRecipe((recipe) => ({
      ...recipe,
      definition: { ...recipe.definition, steps: [...recipe.definition.steps, step] },
    }))
    setSelectedStepId(step.id)
    return step.id
  }

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

  const runFullRecipe = async () => {
    try {
      const project = await persistStudio()
      const recipe = project.recipes.find((item) => item.name === activeRecipe?.name)
      if (!recipe) throw new Error('Save a named recipe before running it')
      await api(`/recipes/${recipe.id}/run`, { method: 'POST' })
      await loadJobs()
      setNotice('Recipe queued as a new job')
    } catch (reason) {
      setError(reason.message)
    }
  }

  const runProjectRecipe = async (recipe) => {
    try {
      await api(`/recipes/${recipe.id}/run`, { method: 'POST' })
      await loadJobs()
      setNotice('Recipe queued as a new job')
      setView('jobs')
    } catch (reason) {
      setError(reason.message)
    }
  }

  const openJob = async (job) => {
    try {
      setJobDetail(await api(`/jobs/${job.id}`))
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
          {['studio', 'projects', 'jobs'].map((item) => (
            <button key={item} className={view === item ? 'nav-tab active' : 'nav-tab'} onClick={() => changeView(item)}>
              {item[0].toUpperCase() + item.slice(1)}
            </button>
          ))}
        </nav>
      </header>

      <main className="main-content">
        {notice && <div className="toast success-toast">{notice}</div>}
        {error && <div className="toast error-toast">{error}</div>}
        {view === 'studio' && (
          <StudioView
            studio={studio}
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
            addClickStep={addClickStep}
            addFillStep={addFillStep}
            saveStudio={saveStudio}
            runFullRecipe={runFullRecipe}
            newRecipe={createRecipe}
          />
        )}
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
        {view === 'jobs' && <JobsView jobs={jobs} jobDetail={jobDetail} openJob={openJob} clearDetail={() => setJobDetail(null)} />}
      </main>
    </div>
  )
}

function StudioView({
  studio, activeRecipe, activeRecipeKey, setActiveRecipeKey, updateRecipe, deleteRecipe, selectedStepId, setSelectedStepId, updateStudio, updateActiveRecipe, updateStep, deleteStep,
  addNavigationStep, addClickStep, addFillStep, saveStudio, runFullRecipe, newRecipe,
}) {
  const [editingRecipeKey, setEditingRecipeKey] = useState(null)
  const [rightMode, setRightMode] = useState('viewer')
  const [pickerActive, setPickerActive] = useState(false)
  const [pickerTargetStepId, setPickerTargetStepId] = useState(null)
  const [pickerAction, setPickerAction] = useState('click')
  const [pickerValue, setPickerValue] = useState('')
  const [pickerTrial, setPickerTrial] = useState({ status: 'idle', selector: '', error: null, trialId: null })
  const [execution, setExecution] = useState({ status: 'idle', frame: null, logs: [], error: null, pickerElement: null, navigationRunId: null, navigationStatus: 'idle' })
  const inputQueueRef = useRef(Promise.resolve())
  const steps = activeRecipe?.definition.steps || []
  const selectedStep = steps.find((step) => step.id === selectedStepId)
  const firstStep = steps[0]
  const navigationReadyToAddActivities = firstStep?.type === STEP_TYPE && Boolean(firstStep.settings?.url?.trim())
  const navigationRunCompleted = navigationReadyToAddActivities
    && execution.navigationStatus === 'completed'
    && Boolean(execution.navigationRunId)

  const runStepLive = async (step) => {
    const stepIndex = steps.findIndex((item) => item.id === step.id)
    if (stepIndex > 0 && !navigationRunCompleted) return
    const interactiveStep = step.type === CLICK_STEP_TYPE || step.type === FILL_STEP_TYPE
    const activeNavigationRunId = interactiveStep && navigationRunCompleted ? execution.navigationRunId : null
    if (interactiveStep) {
      setRightMode('viewer')
      setPickerActive(false)
      if (!activeNavigationRunId) {
        setExecution((current) => ({ ...current, status: 'failed', error: 'Run Navigate to website first so this activity can use the loaded browser.' }))
        return
      }
      setExecution((current) => ({ ...current, status: 'running', stepId: step.id, stepType: step.type, error: null }))
      try {
        await api(`/step-runs/${activeNavigationRunId}/input`, {
          method: 'POST',
          body: JSON.stringify(step.type === FILL_STEP_TYPE
            ? { type: 'selector_fill', selector: step.settings?.selector || '', value: step.settings?.value || '' }
            : { type: 'selector_click', selector: step.settings?.selector || '' }),
        })
      } catch (reason) {
        setExecution((current) => ({ ...current, status: 'failed', error: reason.message }))
      }
      return
    }
    setRightMode('viewer')
    setPickerActive(false)
    if (execution.navigationRunId && execution.navigationStatus === 'completed') {
      setExecution((current) => ({ ...current, runId: current.navigationRunId, status: 'running', stepId: step.id, stepType: step.type, error: null }))
      try {
        await api(`/step-runs/${execution.navigationRunId}/input`, {
          method: 'POST',
          body: JSON.stringify({ type: 'navigate', url: step.settings?.url || '' }),
        })
      } catch (reason) {
        setExecution((current) => ({ ...current, status: 'failed', error: reason.message }))
      }
      return
    }
    setExecution({ status: 'running', frame: null, logs: [], error: null, runId: null, navigationRunId: null, navigationStatus: 'running', pickerElement: null, stepId: step.id, stepType: step.type })
    try {
      let startResponse
      try {
        startResponse = await fetch(`${API_ROOT}/step-runs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ step }),
        })
      } catch {
        throw new Error('Sui backend is offline. Start it with: python main.py')
      }
      const started = await startResponse.json()
      if (!startResponse.ok) throw new Error(started.error || 'Unable to start step')
      setExecution((current) => ({ ...current, runId: started.run_id, navigationRunId: started.run_id, navigationStatus: 'running' }))
      let eventResponse
      try {
        eventResponse = await fetch(`${API_ROOT}/step-runs/${started.run_id}/events`)
      } catch {
        throw new Error('Sui backend is offline. Start it with: python main.py')
      }
      if (!eventResponse.ok || !eventResponse.body) throw new Error('Unable to connect to execution stream')
      const reader = eventResponse.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
        const messages = buffer.split('\n\n')
        buffer = messages.pop() || ''
        for (const message of messages) {
          const line = message.split('\n').find((item) => item.startsWith('data: '))
          if (!line) continue
          const event = JSON.parse(line.slice(6))
          if (event.type === 'frame') setExecution((current) => ({ ...current, frame: event.data }))
          if (event.type === 'log') setExecution((current) => ({ ...current, logs: [...current.logs, event.message] }))
          if (event.type === 'inspect') setExecution((current) => ({ ...current, pickerElement: event.element || null }))
          if (event.type === 'action_complete') {
            if (event.trial_id) setPickerTrial((current) => current.trialId === event.trial_id ? { ...current, status: event.status, error: event.error || null } : current)
            else setExecution((current) => ({ ...current, status: event.status, error: event.error || null, ...(event.action === 'navigate' ? { navigationStatus: event.status } : {}) }))
          }
          if (event.type === 'status') setExecution((current) => ({ ...current, status: event.status, ...(step.type === STEP_TYPE ? { navigationStatus: event.status } : {}) }))
          if (event.type === 'complete') setExecution((current) => ({ ...current, status: event.status, error: event.error || null, ...(step.type === STEP_TYPE ? { navigationStatus: event.status } : {}) }))
        }
        if (done) break
      }
    } catch (reason) {
      setExecution((current) => ({ ...current, status: 'failed', error: reason.message }))
    }
  }

  const sendViewerInput = (payload) => {
    const browserRunId = execution.navigationRunId || execution.runId
    if (!browserRunId) return
    inputQueueRef.current = inputQueueRef.current
      .then(() => api(`/step-runs/${browserRunId}/input`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }))
      .catch((reason) => setExecution((current) => ({ ...current, error: reason.message })))
  }

  const selectStep = (stepId) => {
    setPickerActive(false)
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null })
    setSelectedStepId(stepId)
    setRightMode('editor')
  }
  const activatePicker = (targetStepId = null, action = 'click') => {
    if (!navigationRunCompleted) return
    setRightMode('viewer')
    setPickerActive(true)
    setPickerTargetStepId(targetStepId || (selectedStep?.type === CLICK_STEP_TYPE ? selectedStep.id : null))
    setPickerAction(action)
    const target = targetStepId ? steps.find((step) => step.id === targetStepId) : null
    setPickerValue(target?.settings?.value || '')
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null })
    setExecution((current) => ({ ...current, pickerElement: null, error: null }))
  }
  const beginActivityPicker = (action) => {
    if (!navigationReadyToAddActivities) return
    if (navigationRunCompleted) activatePicker(null, action)
  }
  const choosePickerSelector = (selector) => {
    const value = String(selector || '').trim()
    if (!value) return
    setExecution((current) => ({ ...current, pickerElement: current.pickerElement ? { ...current.pickerElement, selectedSelector: value } : current.pickerElement }))
    setPickerTrial({ status: 'idle', selector: value, error: null, trialId: null })
  }
  const repickElement = () => {
    setExecution((current) => ({ ...current, pickerElement: null }))
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null })
    setPickerActive(true)
  }
  const tryPickerSelector = async () => {
    const selector = String(execution.pickerElement?.selectedSelector || execution.pickerElement?.selector || '').trim()
    const browserRunId = execution.navigationRunId || execution.runId
    if (!selector || !browserRunId) return
    const trialId = makeKey('picker-trial')
    setPickerTrial({ status: 'running', selector, error: null, trialId })
    setPickerActive(false)
    try {
      await api(`/step-runs/${browserRunId}/input`, {
        method: 'POST',
        body: JSON.stringify(pickerAction === 'fill'
          ? { type: 'selector_fill', selector, value: pickerValue, trial_id: trialId }
          : { type: 'selector_click', selector, trial_id: trialId }),
      })
    } catch (reason) {
      setPickerTrial({ status: 'failed', selector, error: reason.message, trialId })
    }
  }
  const cancelPicker = () => {
    setPickerActive(false)
    setPickerTargetStepId(null)
    setPickerAction('click')
    setPickerValue('')
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null })
    setExecution((current) => ({ ...current, pickerElement: null, error: null }))
    setRightMode('viewer')
  }
  const confirmPickedActivity = () => {
    const selector = execution.pickerElement?.selectedSelector || execution.pickerElement?.selector
    if (!selector) return
    const targetStep = steps.find((step) => step.id === pickerTargetStepId)
    const targetType = pickerAction === 'fill' ? FILL_STEP_TYPE : CLICK_STEP_TYPE
    const targetStepId = targetStep?.type === targetType
      ? targetStep.id
      : pickerAction === 'fill' ? addFillStep() : addClickStep()
    updateStep(targetStepId, (current) => ({
      ...current,
      settings: { ...current.settings, selector, ...(pickerAction === 'fill' ? { value: pickerValue } : {}) },
    }))
    setPickerActive(false)
    setPickerTargetStepId(null)
    setPickerAction('click')
    setPickerValue('')
    setPickerTrial({ status: 'idle', selector: '', error: null, trialId: null })
    setRightMode('editor')
  }
  return (
    <section className="studio-layout">
      <aside className="studio-sidebar">
        <div className="eyebrow">Studio</div>
        <div className="section-label">NEW PROJECT</div>
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
            <button className="delete-button" title="Delete recipe" onClick={(event) => { event.stopPropagation(); deleteRecipe(recipe) }}>×</button>
          </div>
        ))}
        {!studio.recipes.length && <div className="empty-sidebar">No recipes</div>}
        <button className="text-button" onClick={newRecipe}>+ New recipe</button>
        <div className="sidebar-actions">
          <button className="primary-button" onClick={saveStudio}>Save</button>
          <button className="secondary-button" onClick={runFullRecipe} disabled={!activeRecipe?.id && !studio.projectId}>Run full recipe</button>
        </div>
      </aside>

      <div className="studio-workspace">
        <div className="canvas-shell">
          <div className="palette">
            <div className="section-label">Blocks</div>
            {steps.map((step, index) => {
              const stepLocked = index > 0 && !navigationRunCompleted
              return (
              <div
                className={`${selectedStepId === step.id ? 'step-list-item selected' : 'step-list-item'}${stepLocked ? ' locked' : ''}`}
                key={step.id}
                role="button"
                tabIndex="0"
                onClick={(event) => {
                  if (stepLocked) return
                  if (event.target.closest('.step-name')) selectStep(step.id)
                  else if (!event.target.closest('button')) setRightMode('viewer')
                }}
                onKeyDown={(event) => { if (!stepLocked && (event.key === 'Enter' || event.key === ' ')) selectStep(step.id) }}
              >
                <span className="step-number">{index + 1}</span>
                <span className="step-name">{stepLabel(step)}</span>
                <button className="step-play" aria-label="Run this step" disabled={stepLocked} onClick={(event) => { event.stopPropagation(); runStepLive(step) }}>▶</button>
              </div>
              )
            })}
            {!steps.length && <div className="empty-sidebar">No steps yet</div>}
            <button className="secondary-button add-button" onClick={() => setRightMode('picker')}>Add step</button>
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
                    onChooseSelector={choosePickerSelector}
                    onChooseAction={(action) => { setPickerAction(action); setPickerTrial((current) => ({ ...current, status: 'idle', error: null, trialId: null })) }}
                    onChangeValue={setPickerValue}
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
                    onInput={sendViewerInput}
                    pickerActive={pickerActive}
                    pickerEnabled={navigationRunCompleted}
                    onPickerToggle={() => activatePicker()}
                  />
                )}
              </div>
            )}
            {rightMode === 'picker' && (
              <div className="step-picker">
                <button className="back-execution" onClick={() => setRightMode('viewer')}>← Back to execution</button>
                <div className="section-label">Available activities</div>
                <button className="picker-option" disabled={Boolean(steps.length && firstStep?.type !== STEP_TYPE)} onClick={() => { addNavigationStep(); setRightMode('editor') }}>
                  <span className="block-icon">↗</span><span>Navigate to website</span>
                </button>
                <button className="picker-option" disabled={!navigationRunCompleted} onClick={() => beginActivityPicker('click')} title={!navigationReadyToAddActivities ? 'Add a URL to the first Navigate to website step first' : 'Run the first Navigate to website step before picking an element'}>
                  <span className="block-icon">+</span><span>Click</span>
                </button>
                <button className="picker-option" disabled={!navigationRunCompleted} onClick={() => beginActivityPicker('fill')} title="Run the first Navigate to website step before picking an element">
                  <span className="block-icon">+</span><span>Fill</span>
                </button>
                <button className="back-to-view" type="button" onClick={() => setRightMode('viewer')}>BACK TO VIEW</button>
              </div>
            )}
            {rightMode === 'editor' && selectedStep && (
              <div className="step-detail">
                <div className="section-label">Selected step</div>
                <div className="step-detail-heading"><h3>{stepLabel(selectedStep)}</h3><button className="step-delete" type="button" onClick={() => { deleteStep(selectedStep.id); setRightMode('viewer') }}>Delete step</button></div>
                {selectedStep.type === CLICK_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Choose an element in the live browser. This step will click it by selector.</p>
                    <label>Selector<input value={selectedStep.settings?.selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, selector: event.target.value } }))} placeholder="Pick an element in the TV" /></label>
                    <button className="secondary-button picker-launch" disabled={!navigationRunCompleted} onClick={() => activatePicker(selectedStep.id, 'click')}>Pick element in TV</button>
                  </>
                ) : selectedStep.type === FILL_STEP_TYPE ? (
                  <>
                    <p className="detail-copy">Choose an element in the live browser and fill it with the value below.</p>
                    <label>Selector<input value={selectedStep.settings?.selector || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, selector: event.target.value } }))} placeholder="Pick an element in the TV" /></label>
                    <label>Value<input value={selectedStep.settings?.value || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, value: event.target.value } }))} placeholder="Text to fill" /></label>
                    <button className="secondary-button picker-launch" disabled={!navigationRunCompleted} onClick={() => activatePicker(selectedStep.id, 'fill')}>Pick element in TV</button>
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
          </div>
        </div>
      </div>
    </section>
  )
}

function SelectorPicker({ execution, pickerTrial, pickerAction, pickerValue, onChooseSelector, onChooseAction, onChangeValue, onTrySelector, onRepick, onConfirm, onCancel }) {
  const element = execution.pickerElement
  const candidates = (Array.isArray(element?.selectorCandidates) ? element.selectorCandidates : [])
    .map((candidate) => typeof candidate === 'string' ? { selector: candidate, kind: 'Selector', unique: null, matches: null } : candidate)
    .filter((candidate) => String(candidate?.selector || '').trim())
  const selectedSelector = String(element?.selectedSelector || pickerTrial.selector || element?.selector || '').trim()
  const trialRunning = pickerTrial.status === 'running'
  const trialFinished = pickerTrial.status === 'completed' || pickerTrial.status === 'failed'
  return (
    <div className={element ? 'selector-picker-panel has-element' : 'selector-picker-panel'} onClick={(event) => event.stopPropagation()}>
      <div className="selector-picker-toolbar">
        <span className="section-label">Pick an element</span>
        <div className="selector-picker-actions">
          <button className={pickerAction === 'click' ? 'selector-action active' : 'selector-action'} type="button" onClick={() => onChooseAction('click')}>Click</button>
          <button className={pickerAction === 'fill' ? 'selector-action active' : 'selector-action'} type="button" onClick={() => onChooseAction('fill')}>Fill</button>
          <button className="selector-action" type="button" disabled title="Scrape is not supported yet">Scrape</button>
        </div>
      </div>
      {!element ? (
        <div className="selector-picker-empty" aria-hidden="true" />
      ) : (
        <>
          <div className="selector-picker-element">
            <strong>{element.metadata?.tag || 'Element'}{element.metadata?.text ? ` · ${element.metadata.text}` : ''}</strong>
            <span>{element.metadata?.ariaLabel || element.metadata?.role || 'Choose a selector to try'}</span>
          </div>
          {pickerAction === 'fill' && <label className="picker-value-field">Value<input value={pickerValue} onChange={(event) => onChangeValue(event.target.value)} placeholder="Text to fill" /></label>}
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
          {pickerTrial.status === 'completed' && <div className="selector-picker-success">{pickerAction === 'fill' ? 'Fill completed with this selector.' : 'Click completed with this selector.'}</div>}
          {trialFinished && <div className="selector-picker-confirm">Confirm this {pickerAction === 'fill' ? 'Fill' : 'Click'} step with the selected selector.</div>}
          <div className="selector-picker-footer">
            <button className="primary-button" type="button" disabled={!selectedSelector || trialRunning || (pickerAction === 'fill' && !pickerValue)} onClick={onTrySelector}>{trialRunning ? 'Trying…' : pickerAction === 'fill' ? 'Try Fill' : 'Try Click'}</button>
            {trialFinished ? <button className="primary-button" type="button" disabled={!selectedSelector || trialRunning || (pickerAction === 'fill' && !pickerValue)} onClick={onConfirm}>Confirm</button> : null}
            <button className="secondary-button" type="button" onClick={onRepick}>Repick</button>
            <button className="secondary-button" type="button" onClick={onCancel}>Cancel</button>
          </div>
        </>
      )}
      {!element && <div className="selector-picker-footer"><button className="secondary-button" type="button" onClick={onCancel}>Cancel</button></div>}
    </div>
  )
}

function ExecutionViewer({ execution, onInput, pickerActive, pickerEnabled, onPickerToggle }) {
  const statusLabel = execution.status === 'idle' ? '' : execution.status === 'completed' ? 'Completed' : execution.status === 'failed' ? 'Failed' : 'Running'
  const handleKeyDown = (event) => {
    if (!onInput || !execution.runId) return
    if (event.key.length === 1) {
      event.preventDefault()
      onInput({ type: 'text', text: event.key })
    } else if (['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) {
      event.preventDefault()
      onInput({ type: 'key', key: event.key })
    }
  }
  const handleImageClick = (event) => {
    if (!onInput || !execution.runId) return
    const image = event.currentTarget
    const rect = image.getBoundingClientRect()
    const sourceWidth = image.naturalWidth || 1280
    const sourceHeight = image.naturalHeight || 720
    const scale = Math.min(rect.width / sourceWidth, rect.height / sourceHeight)
    const renderedWidth = sourceWidth * scale
    const renderedHeight = sourceHeight * scale
    const x = (event.clientX - rect.left - (rect.width - renderedWidth) / 2) / scale
    const y = (event.clientY - rect.top - (rect.height - renderedHeight) / 2) / scale
    if (x >= 0 && y >= 0 && x <= sourceWidth && y <= sourceHeight) onInput({ type: pickerActive ? 'inspect' : 'click', x, y })
  }
  return (
    <div
      className={execution.frame ? 'execution-viewer has-frame' : 'execution-viewer'}
      tabIndex="0"
      onClick={(event) => event.currentTarget.focus()}
      onKeyDown={handleKeyDown}
    >
      {pickerEnabled && !pickerActive && (
        <button className={pickerActive ? 'picker-toggle active' : 'picker-toggle'} onClick={(event) => { event.stopPropagation(); onPickerToggle() }}>
          {pickerActive ? 'Picker on' : 'Picker'}
        </button>
      )}
      {execution.frame ? <img src={execution.frame} alt="Live browser execution" onClick={handleImageClick} /> : <div className="viewer-mark">SUI</div>}
      {statusLabel && <span className={`viewer-status ${execution.status}`}>{statusLabel}</span>}
      {execution.error && <div className="viewer-error">{execution.error}</div>}
    </div>
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

function JobsView({ jobs, jobDetail, openJob, clearDetail }) {
  if (jobDetail) {
    return <section className="page-section"><button className="back-button" onClick={clearDetail}>← Jobs</button><div className="section-heading"><div><span className="eyebrow">Execution</span><h1>{jobDetail.project_name} / {jobDetail.recipe_name}</h1></div><StatusBadge status={jobDetail.status} /></div><div className="job-meta"><span>Started {formatTime(jobDetail.started_at)}</span><span>Ended {formatTime(jobDetail.ended_at)}</span></div>{jobDetail.error && <div className="error-banner">{jobDetail.error}</div>}<div className="log-panel"><div className="section-label">Logs</div>{jobDetail.logs?.length ? jobDetail.logs.map((log) => <div className={`log-line ${log.level}`} key={log.id}><time>{formatTime(log.created_at)}</time><span>{log.message}</span></div>) : <div className="empty-state">No logs yet.</div>}</div></section>
  }
  return <section className="page-section"><div className="section-heading"><div><span className="eyebrow">History</span><h1>Jobs</h1></div></div><div className="job-table"><div className="job-table-header"><span>Project / recipe</span><span>Status</span><span>Started</span><span>Ended</span></div>{jobs.map((job) => <button className="job-row" key={job.id} onClick={() => openJob(job)}><span><strong>{job.project_name}</strong><small>{job.recipe_name}</small></span><StatusBadge status={job.status} /><span>{formatTime(job.started_at)}</span><span>{formatTime(job.ended_at)}</span></button>)}{!jobs.length && <div className="empty-state">No executions yet. Run a recipe from Projects or Studio.</div>}</div></section>
}

function StatusBadge({ status }) {
  return <span className={`status-badge ${status}`}>{status}</span>
}

export default App
