/**
 * 语言检测规则配置
 * 支持智能语言识别的正则表达式模式库
 */

const LanguagePatterns = {
    'python': [
        { regex: /def\s+\w+\s*\(/, weight: 10 },
        { regex: /import\s+\w+/, weight: 8 },
        { regex: /from\s+\w+\s+import/, weight: 8 },
        { regex: /if\s+__name__\s*==\s*['"']__main__['"']/, weight: 15 },
        { regex: /print\s*\(/, weight: 5 },
        { regex: /@\w+/, weight: 6 },
        { regex: /class\s+\w+.*:/, weight: 8 }
    ],

    'javascript': [
        { regex: /function\s+\w+\s*\(/, weight: 8 },
        { regex: /const\s+\w+\s*=/, weight: 6 },
        { regex: /let\s+\w+\s*=/, weight: 6 },
        { regex: /console\.log\s*\(/, weight: 10 },
        { regex: /document\./, weight: 9 },
        { regex: /=>\s*[{(]/, weight: 8 },
        { regex: /require\s*\(/, weight: 8 }
    ],

    'json': [
        { regex: /^\s*\{/, weight: 8 },
        { regex: /"\w+"\s*:\s*"/, weight: 10 },
        { regex: /"\w+"\s*:\s*\d+/, weight: 8 },
        { regex: /"\w+"\s*:\s*true|false/, weight: 12 },
        { regex: /"\w+"\s*:\s*null/, weight: 10 },
        { regex: /^\s*\[.*\]\s*$/, weight: 6 }
    ],

    'html': [
        { regex: /<\w+[^>]*>/, weight: 8 },
        { regex: /<\/\w+>/, weight: 8 },
        { regex: /<!DOCTYPE/i, weight: 15 },
        { regex: /<html/i, weight: 12 }
    ],

    'css': [
        { regex: /\w+\s*\{[^}]*\}/, weight: 10 },
        { regex: /@media/, weight: 12 },
        { regex: /color\s*:/, weight: 6 },
        { regex: /\.[\w-]+\s*\{/, weight: 8 }
    ],

    'sql': [
        { regex: /SELECT\s+/i, weight: 12 },
        { regex: /FROM\s+/i, weight: 10 },
        { regex: /WHERE\s+/i, weight: 8 },
        { regex: /INSERT\s+INTO/i, weight: 12 }
    ],

    'bash': [
        { regex: /^#!/, weight: 15 },
        { regex: /echo\s+/, weight: 6 },
        { regex: /\$\w+/, weight: 5 },
        { regex: /if\s+\[/, weight: 8 }
    ],

        'java': [
        { regex: /public\s+class\s+\w+/, weight: 12 },
        { regex: /import\s+java\./, weight: 10 },
        { regex: /public\s+static\s+void\s+main/, weight: 15 },
        { regex: /System\.out\.println/, weight: 10 },
        { regex: /@Override/, weight: 8 },
        { regex: /extends\s+\w+/, weight: 8 }
    ],

    'typescript': [
        { regex: /interface\s+\w+/, weight: 12 },
        { regex: /:\s*string\s*[;,}]/, weight: 10 },
        { regex: /:\s*number\s*[;,}]/, weight: 10 },
        { regex: /export\s+default/, weight: 8 },
        { regex: /type\s+\w+\s*=/, weight: 8 },
        { regex: /as\s+\w+/, weight: 6 }
    ],

    'php': [
        { regex: /<\?php/, weight: 15 },
        { regex: /\$\w+/, weight: 8 },
        { regex: /echo\s+/, weight: 6 },
        { regex: /function\s+\w+\s*\(/, weight: 8 },
        { regex: /class\s+\w+/, weight: 8 },
        { regex: /->/, weight: 6 }
    ],

    'xml': [
        { regex: /<\?xml/, weight: 15 },
        { regex: /<\w+[^>]*>.*<\/\w+>/, weight: 10 },
        { regex: /xmlns:/, weight: 12 },
        { regex: /<!\[CDATA\[/, weight: 10 },
        { regex: /<!--.*-->/, weight: 6 }
    ],

    'yaml': [
        { regex: /^---/, weight: 15 },
        { regex: /^\s*\w+:\s*$/, weight: 8 },
        { regex: /^\s*-\s+\w+/, weight: 8 },
        { regex: /\|\s*$/, weight: 10 },
        { regex: />\s*$/, weight: 8 }
    ],

    'powershell': [
        { regex: /Get-\w+/, weight: 12 },
        { regex: /Set-\w+/, weight: 10 },
        { regex: /\$\w+/, weight: 6 },
        { regex: /Write-Host/, weight: 10 },
        { regex: /param\s*\(/, weight: 8 },
        { regex: /\|\s*ForEach-Object/, weight: 8 }
    ],

    'rust': [
        { regex: /fn\s+\w+\s*\(/, weight: 12 },
        { regex: /let\s+mut\s+/, weight: 10 },
        { regex: /use\s+std::/, weight: 15 },
        { regex: /\|.*\|\s*\{/, weight: 8 },
        { regex: /impl\s+\w+/, weight: 8 },
        { regex: /match\s+\w+/, weight: 8 }
    ],

    'go': [
        { regex: /package\s+main/, weight: 15 },
        { regex: /func\s+\w+\s*\(/, weight: 10 },
        { regex: /import\s+\(/, weight: 8 },
        { regex: /var\s+\w+\s+\w+/, weight: 6 },
        { regex: /fmt\.Print/, weight: 10 },
        { regex: /:=/, weight: 8 }
    ],

    'swift': [
        { regex: /func\s+\w+\s*\(/, weight: 10 },
        { regex: /var\s+\w+:\s*\w+/, weight: 8 },
        { regex: /let\s+\w+\s*=/, weight: 8 },
        { regex: /print\s*\(/, weight: 6 },
        { regex: /class\s+\w+:\s*\w+/, weight: 10 },
        { regex: /override\s+func/, weight: 8 }
    ],

    'markdown': [
        { regex: /^#{1,6}\s+/, weight: 12 },
        { regex: /\*\*.*\*\*/, weight: 8 },
        { regex: /\*.*\*/, weight: 6 },
        { regex: /\[.*\]\(.*\)/, weight: 10 },
        { regex: /```.*```/, weight: 12 },
        { regex: /^\s*-\s+/, weight: 6 }
    ]
};

// 导出配置对象
if (typeof module !== 'undefined' && module.exports) {
    module.exports = LanguagePatterns;
} else {
    window.LanguagePatterns = LanguagePatterns;
}