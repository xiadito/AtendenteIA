function applyTheme(dark) {
    document.body.classList.toggle('dark', dark);
    const btn = document.getElementById('themeBtn');
    if (btn) btn.textContent = dark ? '☀️ Claro' : '🌙 Escuro';
}

function toggleTheme() {
    const isDark = !document.body.classList.contains('dark');
    localStorage.setItem('theme', isDark ? 'dark' : 'light');
    applyTheme(isDark);
}

(function () {
    const saved = localStorage.getItem('theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(saved ? saved === 'dark' : prefersDark);
})();
