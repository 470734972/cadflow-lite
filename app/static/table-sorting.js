// Make the existing table sort controls discoverable from the column headers.
(function () {
  const mappings = [
    ['#jobs table', '#jobSort', { 0: 'job_id', 8: 'submit_time', 9: 'slots' }],
    ['#users table', '#userSort', { 0: 'user', 1: 'running_jobs', 2: 'pending_jobs', 3: 'exit_jobs', 4: 'running_slots' }],
    ['#queues table', '#queueSort', { 0: 'name', 2: 'running', 3: 'per_user_slots', 4: 'pending', 5: 'suspended', 6: 'utilization' }],
    ['#hosts table', '#hostSort', { 0: 'name', 2: 'running_slots', 3: 'cpu', 4: 'load', 5: 'memory', 6: 'memory_pct', 7: 'tmp' }],
  ];

  mappings.forEach(([tableSelector, selectSelector, columns]) => {
    const table = document.querySelector(tableSelector);
    const select = document.querySelector(selectSelector);
    if (!table || !select) return;
    table.querySelectorAll('thead th').forEach((header, index) => {
      const sort = columns[index];
      if (!sort || !Array.from(select.options).some(option => option.value === sort)) return;
      header.classList.add('sortable-header');
      header.tabIndex = 0;
      header.setAttribute('role', 'button');
      header.setAttribute('aria-label', `按${header.textContent.trim()}排序`);
      const applySort = () => {
        select.value = sort;
        select.dispatchEvent(new Event('change', { bubbles: true }));
      };
      header.addEventListener('click', applySort);
      header.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          applySort();
        }
      });
    });
  });
})();
