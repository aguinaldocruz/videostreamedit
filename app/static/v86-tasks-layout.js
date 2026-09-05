(function(){
  const setup=$('#setup'),topTabs=setup?.querySelector('.setup-tabs'),panels=setup?.querySelector('.setup-tab-panels');
  const tasksTab=topTabs?.querySelector('[data-setup-tab="queue"]'),tasksPanel=panels?.querySelector('[data-setup-panel="queue"]');
  const taskQueue=tasksPanel?.querySelector('.task-queue-maintenance'),indexes=setup?.querySelector('.index-maintenance');
  if(!tasksTab||!tasksPanel||!taskQueue||!indexes)return;
  tasksTab.textContent='Tasks';
  tasksPanel.classList.add('tasks-panel');
  const shell=document.createElement('div');shell.className='tasks-shell';
  shell.innerHTML='<div class="tasks-tabs" role="tablist" aria-label="Task sections"><button type="button" role="tab" data-tasks-tab="queue">Task Queue</button><button type="button" role="tab" data-tasks-tab="indexes">Indexes</button></div><section data-tasks-panel="queue"></section><section data-tasks-panel="indexes"></section>';
  tasksPanel.append(shell);shell.querySelector('[data-tasks-panel="queue"]').append(taskQueue);shell.querySelector('[data-tasks-panel="indexes"]').append(indexes);
  const heading=indexes.querySelector('.split-index-heading p');
  if(heading)heading.textContent='Pending index work starts immediately. Queue Check schedules discovery of file changes made outside VideoStreamEdit; optional schedules automate that discovery.';
  const key='videostreamedit.tasks-tab.v1';
  function activate(name){
    if(!shell.querySelector(`[data-tasks-panel="${name}"]`))name='queue';
    shell.querySelectorAll('[data-tasks-tab]').forEach(button=>{const active=button.dataset.tasksTab===name;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1});
    shell.querySelectorAll('[data-tasks-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.tasksPanel!==name));
    localStorage.setItem(key,name);
    if(name==='queue')taskQueue.querySelector('[data-queue-refresh]')?.click();
    else indexes.querySelector('[data-index-tab].active')?.click();
  }
  shell.querySelectorAll('[data-tasks-tab]').forEach(button=>button.onclick=()=>activate(button.dataset.tasksTab));
  tasksTab.addEventListener('click',()=>activate(localStorage.getItem(key)||'queue'));
  activate(localStorage.getItem(key)||'queue');
})();
