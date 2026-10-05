function paintAuth() {
    const box = document.getElementById('auth-buttons');
    if (!box) return;
    const token = localStorage.getItem('rsg_token');
    const name = localStorage.getItem('rsg_name') || 'Account';
    const admin = localStorage.getItem('rsg_admin') === 'true';
    if (!token) {
        box.innerHTML = '<a href="/login" class="btn btn-outline-dark btn-sm mr-2">Login</a><a href="/signup" class="btn btn-dark btn-sm">Sign Up</a>';
        return;
    }
    box.innerHTML = (admin ? '<a href="/admin" class="btn btn-success btn-sm mr-2">Admin</a>' : '') +
        '<a href="/account" class="btn btn-success btn-sm mr-2">My Account (' + name + ')</a>' +
        '<button class="btn btn-outline-dark btn-sm" onclick="rsgLogout()">Logout</button>';
}
function rsgLogout() {
    localStorage.removeItem('rsg_token');
    localStorage.removeItem('rsg_name');
    localStorage.removeItem('rsg_admin');
    location.href = '/';
}
document.addEventListener('DOMContentLoaded', paintAuth);
