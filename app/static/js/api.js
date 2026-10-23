/**
 * Enterprise Multi-Tenant RAG System
 * Core Client API, Authentication State & Feedback Controller
 * static/js/api.js
 */

(function (window) {
    'use strict';

    // =========================================================================
    // 1. Authentication State Manager
    // =========================================================================
    const Auth = {
        TOKEN_KEY: 'enterprise_token',
        ADMIN_TOKEN_KEY: 'enterprise_admin_token',
        USER_KEY: 'enterprise_user',

        getToken: function () {
            return localStorage.getItem(this.TOKEN_KEY) || localStorage.getItem(this.ADMIN_TOKEN_KEY) || null;
        },

        getUser: function () {
            const raw = localStorage.getItem(this.USER_KEY);
            if (!raw) return null;
            try {
                return JSON.parse(raw);
            } catch (e) {
                console.error('Failed to parse user data from localStorage', e);
                return null;
            }
        },

        setAuth: function (token, user) {
            if (token) {
                localStorage.setItem(this.TOKEN_KEY, token);
                localStorage.setItem(this.ADMIN_TOKEN_KEY, token);
            }
            if (user) {
                localStorage.setItem(this.USER_KEY, typeof user === 'string' ? user : JSON.stringify(user));
            }
        },

        clearAuth: function () {
            localStorage.removeItem(this.TOKEN_KEY);
            localStorage.removeItem(this.ADMIN_TOKEN_KEY);
            localStorage.removeItem(this.USER_KEY);
        },

        isAuthenticated: function () {
            return !!this.getToken();
        },

        getRole: function () {
            const user = this.getUser();
            return user && user.role ? user.role : 'MEMBER';
        },

        isSuperAdmin: function () {
            return this.getRole() === 'SUPER_ADMIN';
        },

        isDeptAdmin: function () {
            return this.getRole() === 'DEPT_ADMIN';
        },

        hasAdminAccess: function () {
            const role = this.getRole();
            return role === 'SUPER_ADMIN' || role === 'DEPT_ADMIN';
        },

        logout: function (redirectUrl = '/enterprise/login') {
            const token = this.getToken();
            if (token) {
                try {
                    fetch('/enterprise/auth/logout', {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${token}` }
                    }).catch(() => {});
                } catch (e) {}
            }
            this.clearAuth();
            if (window.Toast) {
                Toast.info('Signed out', 'You have been safely signed out.');
            }
            setTimeout(() => {
                window.location.href = redirectUrl;
            }, 400);
        }
    };

    // =========================================================================
    // 2. Theme State Manager (Monochrome Light / Dark)
    // =========================================================================
    const Theme = {
        STORAGE_KEY: 'app_theme',

        get: function () {
            const saved = localStorage.getItem(this.STORAGE_KEY);
            if (saved === 'dark' || saved === 'light') return saved;
            return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        },

        set: function (theme) {
            const validTheme = theme === 'dark' ? 'dark' : 'light';
            document.documentElement.setAttribute('data-theme', validTheme);
            localStorage.setItem(this.STORAGE_KEY, validTheme);
            this.updateThemeToggleButtons(validTheme);
        },

        toggle: function () {
            const current = this.get();
            const next = current === 'dark' ? 'light' : 'dark';
            this.set(next);
            return next;
        },

        updateThemeToggleButtons: function (theme) {
            document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
                btn.setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`);
                const icon = btn.querySelector('.theme-toggle-icon');
                if (icon) {
                    icon.textContent = theme === 'dark' ? '☀️' : '🌙';
                }
            });
        },

        init: function () {
            const theme = this.get();
            this.set(theme);
        }
    };

    // =========================================================================
    // 3. Modern Feedback Notifications (SweetAlert2 Integration)
    // =========================================================================
    const Toast = {
        _getSwalMixin: function () {
            if (typeof Swal === 'undefined') return null;
            return Swal.mixin({
                toast: true,
                position: 'top-end',
                showConfirmButton: false,
                timer: 3500,
                timerProgressBar: true,
                didOpen: (toast) => {
                    toast.addEventListener('mouseenter', Swal.stopTimer);
                    toast.addEventListener('mouseleave', Swal.resumeTimer);
                },
                customClass: {
                    popup: 'swal2-monochrome-toast'
                }
            });
        },

        fire: function (icon, title, text) {
            const swalToast = this._getSwalMixin();
            if (swalToast) {
                return swalToast.fire({ icon, title, text });
            } else {
                console.log(`[Toast ${icon.toUpperCase()}] ${title}: ${text || ''}`);
            }
        },

        success: function (title, text = '') { return this.fire('success', title, text); },
        error: function (title, text = '') { return this.fire('error', title, text); },
        warning: function (title, text = '') { return this.fire('warning', title, text); },
        info: function (title, text = '') { return this.fire('info', title, text); }
    };

    const Modal = {
        confirm: async function (title, text, confirmButtonText = 'Yes, Proceed', icon = 'warning') {
            if (typeof Swal === 'undefined') {
                return window.confirm(`${title}\n\n${text}`);
            }
            const isDark = Theme.get() === 'dark';
            const result = await Swal.fire({
                title: title,
                text: text,
                icon: icon,
                showCancelButton: true,
                confirmButtonColor: isDark ? '#F8FAFC' : '#0F172A',
                cancelButtonColor: isDark ? '#374151' : '#E2E8F0',
                confirmButtonText: confirmButtonText,
                cancelButtonText: 'Cancel',
                reverseButtons: true,
                background: isDark ? '#111827' : '#FFFFFF',
                color: isDark ? '#F8FAFC' : '#0F172A',
                customClass: {
                    confirmButton: isDark ? 'swal2-dark-confirm' : 'swal2-light-confirm',
                    cancelButton: isDark ? 'swal2-dark-cancel' : 'swal2-light-cancel'
                }
            });
            return result.isConfirmed;
        },

        alert: function (title, text, icon = 'info') {
            if (typeof Swal === 'undefined') {
                return window.alert(`${title}\n\n${text}`);
            }
            const isDark = Theme.get() === 'dark';
            return Swal.fire({
                title: title,
                text: text,
                icon: icon,
                confirmButtonColor: isDark ? '#F8FAFC' : '#0F172A',
                background: isDark ? '#111827' : '#FFFFFF',
                color: isDark ? '#F8FAFC' : '#0F172A'
            });
        },

        error: function (title, text) {
            return this.alert(title, text, 'error');
        },

        success: function (title, text) {
            return this.alert(title, text, 'success');
        }
    };

    // =========================================================================
    // 4. Centralized fetchAPI() Client
    // =========================================================================
    async function fetchAPI(endpoint, options = {}) {
        const config = { ...options };
        const headers = new Headers(config.headers || {});

        // Attach JWT Bearer Token if user is authenticated
        const token = Auth.getToken();
        if (token && !headers.has('Authorization')) {
            headers.set('Authorization', `Bearer ${token}`);
        }

        // Set default Content-Type to JSON if body is a string or plain object (and not FormData)
        if (config.body && !(config.body instanceof FormData) && !(config.body instanceof URLSearchParams)) {
            if (typeof config.body === 'object') {
                config.body = JSON.stringify(config.body);
            }
            if (!headers.has('Content-Type')) {
                headers.set('Content-Type', 'application/json');
            }
        }

        config.headers = headers;

        try {
            const response = await fetch(endpoint, config);

            // Handle 401 Unauthorized globally
            if (response.status === 401) {
                console.warn(`[fetchAPI] 401 Unauthorized for ${endpoint}`);
                Auth.clearAuth();
                Toast.error('Session Expired', 'Please sign in to continue.');
                setTimeout(() => {
                    const currentPath = window.location.pathname;
                    if (currentPath !== '/login' && currentPath !== '/enterprise/login') {
                        window.location.href = `/enterprise/login?redirect=${encodeURIComponent(currentPath)}`;
                    }
                }, 1000);
                throw new Error('Authentication required or session expired.');
            }

            // Handle 403 Forbidden
            if (response.status === 403) {
                const errJson = await response.json().catch(() => ({}));
                const message = errJson.detail || 'You do not have clearance for this operation.';
                Toast.warning('Access Denied', message);
                throw new Error(message);
            }

            // Handle 204 No Content
            if (response.status === 204) {
                return null;
            }

            // Parse response data
            const contentType = response.headers.get('content-type') || '';
            let data = null;
            if (contentType.includes('application/json')) {
                data = await response.json().catch(() => ({}));
            } else {
                data = await response.text();
            }

            // If response is not OK (4xx, 5xx), format and throw error
            if (!response.ok) {
                let errorMessage = `HTTP Error ${response.status}`;
                if (data && typeof data === 'object') {
                    if (typeof data.detail === 'string') {
                        errorMessage = data.detail;
                    } else if (Array.isArray(data.detail)) {
                        // FastAPI Pydantic validation errors format
                        errorMessage = data.detail.map(e => e.msg || e.message || JSON.stringify(e)).join(', ');
                    } else if (data.message) {
                        errorMessage = data.message;
                    }
                } else if (typeof data === 'string' && data.length > 0) {
                    errorMessage = data;
                }
                throw new Error(errorMessage);
            }

            return data;

        } catch (error) {
            console.error(`[fetchAPI] Error fetching ${endpoint}:`, error);
            throw error;
        }
    }

    // =========================================================================
    // 5. Utility & Security Helpers
    // =========================================================================
    function escapeHTML(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function formatBytes(bytes, decimals = 1) {
        if (!bytes || bytes === 0) return '0 B';
        const k = 1024;
        const dm = decimals < 0 ? 0 : decimals;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
    }

    function formatDate(dateString) {
        if (!dateString) return '-';
        try {
            const d = new Date(dateString);
            return d.toLocaleString(undefined, {
                year: 'numeric',
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
        } catch (e) {
            return dateString;
        }
    }

    // Initialize Theme immediately
    Theme.init();

    // Export to global window scope
    window.Auth = Auth;
    window.Theme = Theme;
    window.Toast = Toast;
    window.Modal = Modal;
    window.fetchAPI = fetchAPI;
    window.escapeHTML = escapeHTML;
    window.formatBytes = formatBytes;
    window.formatDate = formatDate;

})(window);
