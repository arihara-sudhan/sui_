import React, { useEffect, useMemo, useRef, useState } from 'react'

const STEP_TYPE = 'navigate_to_website'
const API_ROOT = 'http://127.0.0.1:8000/api'

const makeKey = (prefix) => `${prefix}-${crypto.randomUUID()}`

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
            addNavigationStep={addNavigationStep}
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
  studio, activeRecipe, activeRecipeKey, setActiveRecipeKey, updateRecipe, deleteRecipe, selectedStepId, setSelectedStepId, updateStudio, updateActiveRecipe, updateStep,
  addNavigationStep, saveStudio, runFullRecipe, newRecipe,
}) {
  const [editingRecipeKey, setEditingRecipeKey] = useState(null)
  const [rightMode, setRightMode] = useState('viewer')
  const [execution, setExecution] = useState({ status: 'idle', frame: null, logs: [], error: null })
  const inputQueueRef = useRef(Promise.resolve())
  const steps = activeRecipe?.definition.steps || []
  const selectedStep = steps.find((step) => step.id === selectedStepId)

  const runStepLive = async (step) => {
    setRightMode('viewer')
    setExecution({ status: 'running', frame: null, logs: [], error: null, runId: null })
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
      setExecution((current) => ({ ...current, runId: started.run_id }))
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
          if (event.type === 'status') setExecution((current) => ({ ...current, status: event.status }))
          if (event.type === 'complete') setExecution((current) => ({ ...current, status: event.status, error: event.error || null }))
        }
        if (done) break
      }
    } catch (reason) {
      setExecution((current) => ({ ...current, status: 'failed', error: reason.message }))
    }
  }

  const sendViewerInput = (payload) => {
    if (!execution.runId) return
    inputQueueRef.current = inputQueueRef.current
      .then(() => api(`/step-runs/${execution.runId}/input`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }))
      .catch((reason) => setExecution((current) => ({ ...current, error: reason.message })))
  }

  const selectStep = (stepId) => {
    setSelectedStepId(stepId)
    setRightMode('editor')
  }
  const showViewerFromBackground = (event) => {
    if (!event.target.closest('button, input, textarea, select, a, .step-name')) setRightMode('viewer')
  }
  return (
    <section className="studio-layout">
      <aside className="studio-sidebar" onClick={showViewerFromBackground}>
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
            {steps.map((step, index) => (
              <div
                className={selectedStepId === step.id ? 'step-list-item selected' : 'step-list-item'}
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
                <span className="step-name">Navigate to website</span>
                <button className="step-play" aria-label="Run this step" onClick={(event) => { event.stopPropagation(); runStepLive(step) }}>▶</button>
              </div>
            ))}
            {!steps.length && <div className="empty-sidebar">No steps yet</div>}
            <button className="secondary-button add-button" onClick={() => setRightMode('picker')}>Add step</button>
          </div>
          <div className="canvas" onClick={showViewerFromBackground}>
            {rightMode === 'viewer' && <ExecutionViewer execution={execution} onInput={sendViewerInput} />}
            {rightMode === 'picker' && (
              <div className="step-picker">
                <button className="back-execution" onClick={() => setRightMode('viewer')}>← Back to execution</button>
                <div className="section-label">Available activities</div>
                <button className="picker-option" onClick={() => { addNavigationStep(); setRightMode('editor') }}>
                  <span className="block-icon">↗</span><span>Navigate to website</span>
                </button>
              </div>
            )}
            {rightMode === 'editor' && selectedStep && (
              <div className="step-detail">
                <button className="back-execution" onClick={() => setRightMode('viewer')}>← Back to execution</button>
                <div className="section-label">Selected step</div>
                <h3>Navigate to website</h3>
                <p className="detail-copy">Open this URL when the recipe runs.</p>
                <label>URL<input value={selectedStep.settings?.url || ''} onChange={(event) => updateStep(selectedStep.id, (current) => ({ ...current, settings: { ...current.settings, url: event.target.value } }))} placeholder="https://example.com" /></label>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}

function ExecutionViewer({ execution, onInput }) {
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
    if (x >= 0 && y >= 0 && x <= sourceWidth && y <= sourceHeight) onInput({ type: 'click', x, y })
  }
  return (
    <div
      className={execution.frame ? 'execution-viewer has-frame' : 'execution-viewer'}
      tabIndex="0"
      onClick={(event) => event.currentTarget.focus()}
      onKeyDown={handleKeyDown}
      title={execution.runId ? 'Click here and type into the live browser' : undefined}
    >
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
  return <article className="recipe-card"><div><span className="card-kicker">Recipe</span><h2>{recipe.name}</h2><p>{recipe.definition?.steps?.length || 0} navigation step{recipe.definition?.steps?.length === 1 ? '' : 's'}</p></div><button className="secondary-button" onClick={() => runRecipe(recipe)}>Run recipe</button></article>
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
