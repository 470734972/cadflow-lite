// Queue summary shortcut: show the real waiting rows collected by bjobs.
(function () {
  const summary = document.querySelector('#queueSummary');
  const baseRenderQueues = window.renderQueues;
  if (!summary || typeof baseRenderQueues !== 'function') return;

  function decoratePendingCard() {
    const card = summary.children[3];
    const pending = allQueues.reduce((sum, queue) => sum + (Number(queue.pending) || 0), 0);
    if (!card) return;
    const actionable = pending > 0;
    card.classList.toggle('queue-summary-action', actionable);
    card.setAttribute('role', actionable ? 'button' : 'status');
    card.setAttribute('aria-label', actionable ? '查看等待中的真实作业' : '当前没有等待作业');
    if (actionable) card.tabIndex = 0;
    else card.removeAttribute('tabindex');
  }

  window.renderQueues = function (rows) {
    baseRenderQueues(rows);
    decoratePendingCard();
  };

  function showPendingJobs() {
    const jobsButton = document.querySelector('.nav[data-view="jobs"]');
    if (!jobsButton) return;
    $('#jobSearch').value = '';
    $('#jobStatus').value = 'PEND';
    $('#jobQueue').value = '';
    $('#jobNode').value = '';
    $('#jobSort').value = 'submit_time';
    activateView(jobsButton);
    jobPage = 1;
    renderJobs();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  summary.addEventListener('click', event => {
    const card = event.target.closest('.queue-summary-action');
    if (card && summary.contains(card)) showPendingJobs();
  });
  summary.addEventListener('keydown', event => {
    if ((event.key === 'Enter' || event.key === ' ') && event.target.closest('.queue-summary-action')) {
      event.preventDefault();
      showPendingJobs();
    }
  });
})();

// Overview KPI: preview the actual PEND rows on hover/focus without leaving the page.
(function () {
  const kpis = document.querySelector('#kpis');
  const baseRenderJobs = window.renderJobs;
  if (!kpis || typeof baseRenderJobs !== 'function') return;

  const escapeHtml = value => String(value ?? '-').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));

  function pendingRows() {
    return allJobs.filter(job => ['PEND', 'PSUSP'].includes(String(job.status || '').toUpperCase()))
      .sort((a, b) => String(b.submit_time || '').localeCompare(String(a.submit_time || '')));
  }

  function openPendingJobs() {
    const jobsButton = document.querySelector('.nav[data-view="jobs"]');
    if (!jobsButton) return;
    $('#jobSearch').value = '';
    $('#jobStatus').value = 'PEND';
    $('#jobQueue').value = '';
    $('#jobNode').value = '';
    $('#jobSort').value = 'submit_time';
    activateView(jobsButton);
    jobPage = 1;
    renderJobs();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function decoratePendingKpi() {
    const card = kpis.children[1];
    if (!card) return;
    const rows = pendingRows();
    card.classList.add('pending-kpi');
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-label', rows.length ? `查看 ${rows.length} 个等待作业` : '当前没有等待作业');
    let details = kpis.querySelector('.pending-kpi-details');
    if (!details) {
      // Keep the preview in the KPI grid flow.  An absolutely positioned
      // tooltip covered the resource gauges on shorter viewports.
      details = document.createElement('div');
      details.className = 'pending-kpi-details';
      details.setAttribute('role', 'region');
      details.setAttribute('aria-label', '真实等待作业');
      details.hidden = true;
      kpis.append(details);
      let hideTimer;
      const show = () => { clearTimeout(hideTimer); details.hidden = false; };
      const hide = () => { clearTimeout(hideTimer); hideTimer = setTimeout(() => { details.hidden = true; }, 140); };
      card.addEventListener('mouseenter', show);
      card.addEventListener('mouseleave', hide);
      card.addEventListener('focusin', show);
      card.addEventListener('focusout', event => {
        if (!card.contains(event.relatedTarget) && !details.contains(event.relatedTarget)) hide();
      });
      details.addEventListener('mouseenter', () => clearTimeout(hideTimer));
      details.addEventListener('mouseleave', hide);
      card.addEventListener('click', event => {
        if (!event.target.closest('.pending-kpi-details')) openPendingJobs();
      });
      card.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openPendingJobs();
        }
      });
    }
    const visible = rows.slice(0, 8);
    details.innerHTML = `<div class="pending-kpi-details-head"><strong>真实等待作业（PEND / PSUSP）</strong><small>${rows.length} 个</small></div>${visible.length ? `<ul>${visible.map(job => `<li><strong>#${escapeHtml(job.job_id)}</strong><span>${escapeHtml(job.job_name || '-')}</span><small>${escapeHtml(job.status || '-')} · ${escapeHtml(job.user || '-')} · ${escapeHtml(job.queue || '-')} · 提交 ${escapeHtml(job.submit_host || '-')}</small>${job.pending_reason ? `<small class="pending-reason-text" title="${escapeHtml(job.pending_reason)}">原因：${escapeHtml(job.pending_reason)}</small>` : ''}</li>`).join('')}</ul>${rows.length > visible.length ? `<p>还有 ${rows.length - visible.length} 个，点击卡片查看全部</p>` : '<p>点击卡片查看作业明细</p>'}` : '<p class="pending-kpi-empty">当前没有采集到等待作业</p>'}`;
  }

  window.renderJobs = function (rows) {
    baseRenderJobs(rows);
    decoratePendingKpi();
  };
  decoratePendingKpi();
})();
