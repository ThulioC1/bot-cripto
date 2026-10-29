module.exports = {
  apps: [
    {
      name: "krypto-bot",
      cwd: __dirname,
      script: "main.py",
      interpreter: `${__dirname}/.venv/bin/python`,
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      stop_exit_codes: [0, 78],
      restart_delay: 10000,
      max_restarts: 10,
      min_uptime: "60s",
      kill_timeout: 40000,
      time: true,
      merge_logs: true,
      out_file: `${__dirname}/logs/pm2-out.log`,
      error_file: `${__dirname}/logs/pm2-error.log`,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
