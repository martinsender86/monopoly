#!/usr/bin/env python3
"""MonoPoly — interfaz gráfica.
Unir monos -> poly / separar poly -> monos (bit-exacto) y editar metadata (siempre en copia nueva).
"""
import os
import queue
import re
import struct
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import wavpoly as W

APP = 'MonoPoly'
VERSION = '1.5'

try:                                    # arrastrar y soltar (opcional)
    from tkinterdnd2 import TkinterDnD, DND_FILES
    BaseTk = TkinterDnD.Tk
except Exception:                       # noqa: BLE001
    TkinterDnD, DND_FILES = None, None
    BaseTk = tk.Tk

# ------------------------------------------------------------------ estilo
BG = '#17181b'
PANEL = '#212227'
PANEL2 = '#2a2c32'
LINE = '#34363d'
FG = '#e7e8ea'
DIM = '#8b8f98'
ACCENT = '#4c8dff'
ACCENT_H = '#6aa1ff'
OK = '#3ec27a'
WARN = '#f0b44c'
ERR = '#ef5b5b'
IS_MAC = sys.platform == 'darwin'
_F = 'Segoe UI' if sys.platform == 'win32' else ('Helvetica Neue' if IS_MAC else 'Helvetica')
_S = 13 if IS_MAC else 10              # en Mac Tk dibuja los puntos más chicos
FONT = (_F, _S)
FONT_B = (_F, _S, 'bold')
FONT_T = (_F, _S - 1, 'bold')
ROW_H = 30 if IS_MAC else 26

FPS_CHOICES = list(W.FPS_RATES.keys())
MIXED = '‹varios›'
NO_FPS = '(sin definir)'


def fps_from_choice(s):
    rate, flag = W.FPS_RATES[s]
    return W.parse_rate(rate), flag == 'DF'


def fps_label(fps, drop):
    return W.rate_label(fps, drop)


def ixml_field(w, tag):
    return w.ixml_get(tag).strip()


def bext_date(w):
    b = w.chunk_bodies.get(b'bext')
    if not b or len(b) < 338:
        return ''
    d = b[320:330].decode('ascii', 'replace').strip('\0 ')
    t = b[330:338].decode('ascii', 'replace').strip('\0 ')
    return f'{d} {t}'.strip()


ENTRY_FIELDS = ('SCENE', 'TAKE', 'PROJECT', 'TAPE', 'UBITS')
CHECK_FIELDS = ('CIRCLED', 'WILD_TRACK')


# ------------------------------------------------------------------ app
class App:
    def __init__(self, root):
        self.root = root
        self.files = {}            # iid -> Wav
        self.q = queue.Queue()
        self.busy = False
        self.edit_ids = ()
        self.init = {}
        self.track_edits = {}
        self._loading = False
        self._suppress_sel = False
        root.title(f'{APP} {VERSION} — Unir / Separar / Metadata BWF')
        root.geometry('1320x860')
        root.minsize(1060, 700)
        root.configure(bg=BG)
        self._style()
        self._build()
        if DND_FILES:
            try:
                root.drop_target_register(DND_FILES)
                root.dnd_bind('<<Drop>>', self._on_drop)
            except Exception:      # noqa: BLE001
                pass
        root.after(100, self._poll)

    # -------------------------------------------------------------- estilo
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use('clam')
        s.configure('.', background=BG, foreground=FG, font=FONT, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, troughcolor=PANEL, focuscolor=ACCENT)
        s.configure('TFrame', background=BG)
        s.configure('Panel.TFrame', background=PANEL)
        s.configure('TLabel', background=BG, foreground=FG)
        s.configure('Key.TLabel', background=PANEL, foreground=DIM)
        s.configure('Val.TLabel', background=PANEL, foreground=FG, font=FONT_B)
        s.configure('Title.TLabel', background=PANEL, foreground=ACCENT, font=FONT_T)
        s.configure('Status.TLabel', background=BG, foreground=DIM)
        s.configure('Dirty.TLabel', background=BG, foreground=WARN, font=FONT_B)
        s.configure('Head.TLabel', background=BG, foreground=FG, font=(_F, _S + 5, 'bold'))
        s.configure('Sec.TLabel', background=BG, foreground=FG, font=FONT_B)
        s.configure('Sub.TLabel', background=BG, foreground=DIM)
        s.configure('TButton', background=PANEL2, foreground=FG, borderwidth=0,
                    padding=(14, 7), relief='flat')
        s.map('TButton', background=[('disabled', PANEL), ('active', LINE)],
              foreground=[('disabled', '#5c6068')])
        s.configure('Accent.TButton', background=ACCENT, foreground='white', font=FONT_B)
        s.map('Accent.TButton', background=[('disabled', PANEL2), ('active', ACCENT_H)])
        for st, bg in (('TCheckbutton', BG), ('Panel.TCheckbutton', PANEL)):
            s.configure(st, background=bg, foreground=FG)
            s.map(st, background=[('active', bg)], foreground=[('disabled', DIM)],
                  indicatorcolor=[('alternate', DIM), ('selected', ACCENT), ('!selected', PANEL2)])
        s.configure('TRadiobutton', background=BG, foreground=FG)
        s.map('TRadiobutton', background=[('active', BG)],
              indicatorcolor=[('selected', ACCENT), ('!selected', PANEL2)])
        s.configure('TEntry', fieldbackground=PANEL2, foreground=FG, insertcolor=FG,
                    bordercolor=LINE, padding=(6, 2))
        s.map('TEntry', fieldbackground=[('readonly', PANEL), ('disabled', PANEL)],
              foreground=[('readonly', DIM), ('disabled', DIM)], bordercolor=[('focus', ACCENT)])
        s.configure('TCombobox', fieldbackground=PANEL2, background=PANEL2, foreground=FG,
                    arrowcolor=FG, bordercolor=LINE, padding=3)
        s.map('TCombobox', fieldbackground=[('readonly', PANEL2), ('disabled', PANEL)],
              foreground=[('readonly', FG), ('disabled', DIM)],
              selectbackground=[('readonly', PANEL2)], selectforeground=[('readonly', FG)])
        self.root.option_add('*TCombobox*Listbox.background', PANEL2)
        self.root.option_add('*TCombobox*Listbox.foreground', FG)
        self.root.option_add('*TCombobox*Listbox.selectBackground', ACCENT)
        s.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=FG,
                    rowheight=ROW_H, borderwidth=0)
        s.map('Treeview', background=[('selected', '#2f4d80')], foreground=[('selected', 'white')])
        s.configure('Treeview.Heading', background=PANEL2, foreground=DIM, relief='flat',
                    font=FONT_T, padding=(6, 6))
        s.map('Treeview.Heading', background=[('active', LINE)])
        s.configure('Horizontal.TProgressbar', background=ACCENT, troughcolor=PANEL,
                    bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
        for o in ('Vertical', 'Horizontal'):
            s.configure(f'{o}.TScrollbar', background=PANEL2, troughcolor=PANEL,
                        arrowcolor=DIM, bordercolor=PANEL)

    # -------------------------------------------------------------- layout
    def _build(self):
        r = self.root
        top = ttk.Frame(r, padding=(18, 14, 18, 6))
        top.pack(fill='x')
        ttk.Label(top, text=APP, style='Head.TLabel').pack(side='left')
        ttk.Label(top, text='   Unir monos en poly  ·  Separar poly en monos  ·  Editar metadata',
                  style='Sub.TLabel').pack(side='left', pady=(5, 0))

        bar = ttk.Frame(r, padding=(18, 6))
        bar.pack(fill='x')
        ttk.Button(bar, text='＋ Archivos', command=self.add_files).pack(side='left')
        ttk.Button(bar, text='＋ Carpeta', command=self.add_folder).pack(side='left', padx=(6, 0))
        ttk.Button(bar, text='Quitar', command=self.remove_sel).pack(side='left', padx=(6, 0))
        ttk.Button(bar, text='Limpiar', command=self.clear).pack(side='left', padx=(6, 0))
        self.btn_split = ttk.Button(bar, text='Separar  ⇢ monos', style='Accent.TButton',
                                    command=self.do_split)
        self.btn_split.pack(side='right')
        self.btn_merge = ttk.Button(bar, text='Unir  ⇢ poly', style='Accent.TButton',
                                    command=self.do_merge)
        self.btn_merge.pack(side='right', padx=(0, 8))

        opt = ttk.Frame(r, padding=(18, 2, 18, 4))
        opt.pack(fill='x')
        ttk.Label(opt, text='Agrupar por:', style='Sub.TLabel').pack(side='left')
        self.by_name = tk.BooleanVar(value=True)
        self.by_tc = tk.BooleanVar(value=True)
        self.by_dur = tk.BooleanVar(value=True)
        self.cross = tk.BooleanVar(value=False)
        for t, v in (('Nombre', self.by_name), ('Timecode', self.by_tc), ('Duración', self.by_dur),
                     ('Entre carpetas', self.cross)):
            ttk.Checkbutton(opt, text=t, variable=v).pack(side='left', padx=(8, 0))
        ttk.Separator(opt, orient='vertical').pack(side='left', fill='y', padx=14)
        ttk.Label(opt, text='Salida:', style='Sub.TLabel').pack(side='left')
        self.out_mode = tk.StringVar(value='sub')
        ttk.Radiobutton(opt, text='Subcarpeta POLY / MONO / META', value='sub',
                        variable=self.out_mode).pack(side='left', padx=(8, 0))
        ttk.Radiobutton(opt, text='Misma carpeta', value='same',
                        variable=self.out_mode).pack(side='left', padx=(8, 0))
        ttk.Radiobutton(opt, text='Otra…', value='custom', variable=self.out_mode,
                        command=self.pick_out).pack(side='left', padx=(8, 0))
        self.out_dir = tk.StringVar(value='')
        ttk.Label(opt, textvariable=self.out_dir, style='Sub.TLabel').pack(side='left', padx=(6, 0))
        opt = ttk.Frame(r, padding=(18, 0, 18, 8))
        opt.pack(fill='x')
        ttk.Label(opt, text='Al separar, nombrar por:', style='Sub.TLabel').pack(side='left')
        self.split_names = tk.StringVar(value='num_name')
        for txt, val in (('Tr + nombre (Tr1_Boom)', 'num_name'), ('Solo Tr (Tr1)', 'num'), ('Solo nombre (Boom)', 'name')):
            ttk.Radiobutton(opt, text=txt, value=val, variable=self.split_names).pack(side='left', padx=(8, 0))
        self.fps = tk.StringVar(value='24')
        cb = ttk.Combobox(opt, values=FPS_CHOICES, textvariable=self.fps, width=9, state='readonly')
        cb.pack(side='right')
        cb.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        ttk.Label(opt, text='FPS para mostrar el TC si el archivo no lo trae:', style='Sub.TLabel').pack(side='right', padx=(0, 6))

        # estado (abajo de todo, se empaqueta antes para que nunca quede tapado)
        st = ttk.Frame(r, padding=(18, 8, 18, 12))
        st.pack(side='bottom', fill='x')
        self.prog = ttk.Progressbar(st, mode='determinate', maximum=1000)
        self.prog.pack(fill='x')
        self.status = tk.StringVar(value='Listo.')
        ttk.Label(st, textvariable=self.status, style='Status.TLabel').pack(anchor='w', pady=(6, 0))

        pw = ttk.PanedWindow(r, orient='vertical')
        pw.pack(fill='both', expand=True, padx=18)
        tf = ttk.Frame(pw, style='Panel.TFrame')
        cols = [('name', 'Archivo', 260), ('ch', 'Canales', 80), ('sr', 'Sample Rate', 90),
                ('bits', 'Bits', 50), ('fps', 'Frame Rate', 85), ('tc', 'TC inicio', 105),
                ('len', 'Duración', 95), ('end', 'TC fin', 105), ('scene', 'Escena', 70),
                ('take', 'Toma', 55), ('tape', 'Tape', 80), ('date', 'Fecha', 140), ('dir', 'Carpeta', 300)]
        self.cols = [c[0] for c in cols]
        self.tree = ttk.Treeview(tf, columns=self.cols, show='headings', selectmode='extended', height=8)
        for key, txt, wdt in cols:
            self.tree.heading(key, text=txt, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=wdt, anchor='w' if key in ('name', 'dir', 'date') else 'center',
                             stretch=key in ('name', 'dir'))
        self.tree.tag_configure('odd', background='#25272c')
        self.tree.tag_configure('poly', foreground='#9cc3ff')
        vs = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        hs = ttk.Scrollbar(tf, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Delete>', lambda e: self.remove_sel())
        self.tree.bind('<BackSpace>', lambda e: self.remove_sel())
        self.empty = tk.Label(tf, text='Agregá archivos o carpetas' + (' (o arrastralos acá)' if DND_FILES else ''),
                              bg=PANEL, fg=DIM, font=(_F, _S + 2))
        self.empty.place(relx=.5, rely=.5, anchor='center')
        pw.add(tf, weight=3)

        # ---------------- editor de metadata
        info = ttk.Frame(pw, padding=(0, 8, 0, 0))
        pw.add(info, weight=2)
        hdr = ttk.Frame(info)
        hdr.grid(row=0, column=0, columnspan=4, sticky='ew', pady=(0, 8))
        ttk.Label(hdr, text='METADATA', style='Sec.TLabel').pack(side='left')
        self.edit_hint = tk.StringVar(value='')
        ttk.Label(hdr, textvariable=self.edit_hint, style='Sub.TLabel').pack(side='left', padx=(10, 0))
        self.dirty_lbl = ttk.Label(hdr, text='', style='Dirty.TLabel')
        self.dirty_lbl.pack(side='left', padx=(12, 0))
        self.btn_save = ttk.Button(hdr, text='Guardar copia con cambios', style='Accent.TButton',
                                   command=self.save_meta)
        self.btn_save.pack(side='right')
        self.btn_discard = ttk.Button(hdr, text='Descartar', command=self.discard)
        self.btn_discard.pack(side='right', padx=(0, 8))
        for i, wgt in enumerate((3, 3, 4, 3)):
            info.columnconfigure(i, weight=wgt, uniform='p')
        info.rowconfigure(1, weight=1)

        self.v = {k: tk.StringVar() for k in ENTRY_FIELDS + CHECK_FIELDS + ('FPS', 'TC')}
        self.lbl = {k: tk.StringVar() for k in ('Archivo', 'Canales', 'Sample rate', 'Bit depth',
                                                 'Duración', 'Samples', 'Formato')}
        self.preserve = tk.BooleanVar(value=True)
        self.w = {}

        g = self._panel(info, 0, 'INFO GENERAL')
        self._row_label(g, 1, 'Archivo', 'Archivo')
        self._row_label(g, 2, 'Canales', 'Canales')
        self._row_entry(g, 3, 'Escena', 'SCENE')
        self._row_entry(g, 4, 'Toma', 'TAKE')
        self._row_check(g, 5, 'Circulado', 'CIRCLED')
        self._row_check(g, 6, 'Wild track', 'WILD_TRACK')
        self._row_entry(g, 7, 'Proyecto', 'PROJECT')
        self._row_entry(g, 8, 'Tape', 'TAPE')

        rc = self._panel(info, 1, 'GRABACIÓN')
        self._row_label(rc, 1, 'Sample rate', 'Sample rate')
        self._row_label(rc, 2, 'Bit depth', 'Bit depth')
        ttk.Label(rc, text='Frame rate', style='Key.TLabel').grid(row=3, column=0, sticky='e', padx=(0, 10), pady=1)
        cbf = ttk.Combobox(rc, values=FPS_CHOICES, textvariable=self.v['FPS'], width=11, state='readonly')
        cbf.grid(row=3, column=1, sticky='w', pady=1)
        cbf.bind('<<ComboboxSelected>>', lambda e: self._fps_changed())
        self.w['FPS'] = cbf
        pc = ttk.Checkbutton(rc, text='Preservar TC inicio', variable=self.preserve, style='Panel.TCheckbutton',
                             command=self._preserve_changed)
        pc.grid(row=4, column=1, sticky='w', pady=1)
        self.w['PRESERVE'] = pc
        ttk.Label(rc, text='TC inicio', style='Key.TLabel').grid(row=5, column=0, sticky='e', padx=(0, 10), pady=1)
        e = ttk.Entry(rc, textvariable=self.v['TC'], width=13, state='readonly')
        e.grid(row=5, column=1, sticky='w', pady=1)
        self.w['TC'] = e
        self._row_label(rc, 6, 'Duración', 'Duración')
        self._row_label(rc, 7, 'Samples 00:00', 'Samples')
        self._row_entry(rc, 8, 'UBits', 'UBITS')
        self._row_label(rc, 9, 'Formato', 'Formato')

        tp = self._panel(info, 2, 'TRACKS  (doble clic en el nombre para editar)')
        self.tracks = ttk.Treeview(tp, columns=('ch', 'il', 'name'), show='headings', height=6)
        for k, t, wd in (('ch', 'Canal', 60), ('il', 'Interleave', 80), ('name', 'Nombre', 160)):
            self.tracks.heading(k, text=t)
            self.tracks.column(k, width=wd, anchor='center' if k != 'name' else 'w', stretch=k == 'name')
        self.tracks.grid(row=1, column=0, columnspan=2, sticky='nsew')
        tp.rowconfigure(1, weight=1)
        tp.columnconfigure(0, weight=1)
        self.tracks.bind('<Double-1>', self._edit_track)

        npn = self._panel(info, 3, 'NOTAS')
        self.notes = tk.Text(npn, bg=PANEL2, fg=FG, relief='flat', wrap='word', height=6,
                             font=FONT, insertbackground=FG, padx=8, pady=6,
                             highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
        self.notes.grid(row=1, column=0, columnspan=2, sticky='nsew')
        npn.rowconfigure(1, weight=1)
        npn.columnconfigure(0, weight=1)
        self.notes.bind('<<Modified>>', self._notes_modified)

        for k in self.v:
            self.v[k].trace_add('write', lambda *a: self._update_dirty())
        self._set_editable(False)

    def _panel(self, parent, col, title):
        f = ttk.Frame(parent, style='Panel.TFrame', padding=(12, 10))
        f.grid(row=1, column=col, sticky='nsew', padx=(0, 10 if col < 3 else 0))
        ttk.Label(f, text=title, style='Title.TLabel').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 4))
        f.columnconfigure(1, weight=1)
        return f

    def _row_label(self, f, row, key, var):
        ttk.Label(f, text=key, style='Key.TLabel').grid(row=row, column=0, sticky='e', padx=(0, 10), pady=1)
        ttk.Label(f, textvariable=self.lbl[var], style='Val.TLabel', wraplength=200).grid(
            row=row, column=1, sticky='w', pady=1)

    def _row_entry(self, f, row, key, field):
        ttk.Label(f, text=key, style='Key.TLabel').grid(row=row, column=0, sticky='e', padx=(0, 10), pady=1)
        e = ttk.Entry(f, textvariable=self.v[field], width=18)
        e.grid(row=row, column=1, sticky='ew', pady=1)
        self.w[field] = e

    def _row_check(self, f, row, key, field):
        ttk.Label(f, text=key, style='Key.TLabel').grid(row=row, column=0, sticky='e', padx=(0, 10), pady=1)
        c = ttk.Checkbutton(f, variable=self.v[field], onvalue='TRUE', offvalue='FALSE',
                            style='Panel.TCheckbutton')
        c.grid(row=row, column=1, sticky='w', pady=1)
        self.w[field] = c

    # -------------------------------------------------------------- archivos
    def add_files(self):
        ps = filedialog.askopenfilenames(title='Agregar WAV/BWF',
                                         filetypes=[('WAV / BWF', '*.wav *.WAV *.bwf *.BWF'), ('Todos', '*.*')])
        self.load(list(ps))

    def add_folder(self):
        d = filedialog.askdirectory(title='Agregar carpeta (incluye subcarpetas)')
        if d:
            self.load([d])

    def _on_drop(self, e):
        self.load(list(self.root.tk.splitlist(e.data)))

    def load(self, paths):
        files = []
        for p in paths:
            if os.path.isdir(p):
                for rt, _, fs in os.walk(p):
                    files += [os.path.join(rt, x) for x in fs if x.lower().endswith(('.wav', '.bwf'))]
            elif os.path.isfile(p):
                files.append(p)
        known = {os.path.normcase(os.path.abspath(w.path)) for w in self.files.values()}
        bad = []
        for p in sorted(files, key=W.natkey):
            if os.path.normcase(os.path.abspath(p)) in known:
                continue
            try:
                w = W.Wav(p)
            except (W.WavError, OSError, struct.error) as e:
                bad.append(f'{os.path.basename(p)}: {e}')
                continue
            self.files[f'f{len(self.files)}_{id(w)}'] = w
        self.refresh()
        msg = f'{len(self.files)} archivos cargados.'
        if bad:
            msg += f'  {len(bad)} no se pudieron leer.'
            messagebox.showwarning(APP, 'No se pudieron leer:\n\n' + '\n'.join(bad[:30]))
        self.status.set(msg)

    def remove_sel(self):
        if not self._confirm_discard():
            return
        for iid in self.tree.selection():
            self.files.pop(iid, None)
        self.refresh()

    def clear(self):
        if not self._confirm_discard():
            return
        self.files.clear()
        self.refresh()

    def pick_out(self):
        d = filedialog.askdirectory(title='Carpeta de salida')
        if d:
            self.out_dir.set(d)
        elif not self.out_dir.get():
            self.out_mode.set('sub')

    # -------------------------------------------------------------- tabla
    def _fps(self, w):
        fps, flag = w.ixml_fps()
        if fps:
            return fps, flag == 'DF', True
        f, d = fps_from_choice(self.fps.get())
        return f, d, False

    def row(self, w):
        fps, drop, real = self._fps(w)
        tr = w.time_reference
        return (os.path.basename(w.path),
                'MONO' if w.channels == 1 else f'{w.channels} (POLY)',
                w.sr, w.bits, fps_label(fps, drop) + ('' if real else ' *'),
                W.tc_string(tr, w.sr, fps, drop) if tr is not None else '--',
                W.tc_string(w.frames, w.sr, fps, False),
                W.tc_string(tr + w.frames, w.sr, fps, drop) if tr is not None else '--',
                ixml_field(w, 'SCENE'), ixml_field(w, 'TAKE'), ixml_field(w, 'TAPE'),
                bext_date(w), os.path.dirname(w.path))

    def refresh(self):
        sel = set(self.tree.selection())
        self._suppress_sel = True
        self.tree.delete(*self.tree.get_children())
        for i, (iid, w) in enumerate(self.files.items()):
            tags = ['odd'] if i % 2 else []
            if w.channels > 1:
                tags.append('poly')
            self.tree.insert('', 'end', iid=iid, values=self.row(w), tags=tags)
        keep = [s for s in sel if self.tree.exists(s)]
        if keep:
            self.tree.selection_set(keep)
        self.root.update_idletasks()
        self._suppress_sel = False
        if self.files:
            self.empty.place_forget()
        else:
            self.empty.place(relx=.5, rely=.5, anchor='center')
        self.load_editor(tuple(self.tree.selection()))

    def sort_by(self, key):
        idx = self.cols.index(key)
        items = list(self.files.items())
        rev = getattr(self, '_sort', None) == key
        items.sort(key=lambda kv: W.natkey(str(self.row(kv[1])[idx])), reverse=rev)
        self._sort = None if rev else key
        self.files = dict(items)
        self.refresh()

    # -------------------------------------------------------------- editor
    def _on_select(self, _e=None):
        if self._suppress_sel:
            return
        new = tuple(self.tree.selection())
        if new == self.edit_ids:
            return
        if self.is_dirty() and not messagebox.askyesno(
                APP, 'Hay cambios de metadata sin guardar.\n\n¿Descartarlos?', icon='warning'):
            self._suppress_sel = True
            self.tree.selection_set(self.edit_ids)
            self.root.after(10, lambda: setattr(self, '_suppress_sel', False))
            return
        self.load_editor(new)

    def _confirm_discard(self):
        return not self.is_dirty() or messagebox.askyesno(
            APP, 'Hay cambios de metadata sin guardar.\n\n¿Descartarlos?', icon='warning')

    def _common(self, values):
        vals = set(values)
        return vals.pop() if len(vals) == 1 else MIXED

    def _set_editable(self, on, single=False):
        for k in ENTRY_FIELDS:
            self.w[k].state(['!disabled'] if on else ['disabled'])
        for k in CHECK_FIELDS:
            self.w[k].state(['!disabled'] if on else ['disabled'])
        self.w['FPS'].state(['!disabled', 'readonly'] if on else ['disabled'])
        self.w['PRESERVE'].state(['!disabled'] if (on and single) else ['disabled'])
        self.notes.configure(state='normal' if on else 'disabled',
                             bg=PANEL2 if on else PANEL)

    def load_editor(self, ids):
        self._loading = True
        self.edit_ids = ids
        self.track_edits = {}
        wavs = [self.files[i] for i in ids if i in self.files]
        for v in list(self.v.values()) + list(self.lbl.values()):
            v.set('')
        self.preserve.set(True)
        self.w['TC'].configure(state='readonly')
        self.tracks.delete(*self.tracks.get_children())
        self.notes.configure(state='normal')
        self.notes.delete('1.0', 'end')
        self.init = {}
        if not wavs:
            self.edit_hint.set('seleccioná uno o varios archivos para ver / editar')
            self._set_editable(False)
            self._finish_load()
            return
        single = len(wavs) == 1
        self.edit_hint.set(os.path.basename(wavs[0].path) if single
                           else f'{len(wavs)} archivos seleccionados — los cambios se aplican a todos')
        for k in ENTRY_FIELDS:
            self.v[k].set(self._common(ixml_field(w, k) for w in wavs))
        for k in CHECK_FIELDS:
            c = self._common(ixml_field(w, k).upper() for w in wavs)
            self.v[k].set(c if c in ('TRUE', 'FALSE') else ('FALSE' if c == '' else ''))
        fpss = []
        for w in wavs:
            f, flag = w.ixml_fps()
            fpss.append(fps_label(f, flag == 'DF') if f else NO_FPS)
        cf = self._common(fpss)
        self.v['FPS'].set(cf)
        note = self._common(ixml_field(w, 'NOTE') for w in wavs)
        self.notes.insert('1.0', note)
        w0 = wavs[0]
        if single:
            fps, drop, _ = self._fps(w0)
            self.v['TC'].set(W.tc_string(w0.time_reference or 0, w0.sr, fps, drop))
            self.lbl['Archivo'].set(os.path.basename(w0.path))
            self.lbl['Canales'].set('1 (MONO)' if w0.channels == 1 else f'{w0.channels} (POLY)')
            self.lbl['Duración'].set(W.tc_string(w0.frames, w0.sr, fps))
            self.lbl['Samples'].set('--' if w0.time_reference is None
                                    else f'{w0.time_reference:,}'.replace(',', '.'))
            tr = w0.ixml_tracks()
            rows = [(t.get('CHANNEL_INDEX', ''), t.get('INTERLEAVE_INDEX', ''), t.get('NAME', ''))
                    for t in tr] if tr else [(i + 1, i + 1, '') for i in range(w0.channels)]
            for r_ in rows:
                self.tracks.insert('', 'end', values=r_)
        else:
            tcs = {(w.time_reference, w.sr) for w in wavs}
            self.v['TC'].set(MIXED if len(tcs) > 1 else
                             W.tc_string(w0.time_reference or 0, w0.sr, *self._fps(w0)[:2]))
            self.lbl['Archivo'].set(f'{len(wavs)} archivos')
            self.lbl['Canales'].set(self._common(str(w.channels) for w in wavs))
            self.lbl['Duración'].set(MIXED if len({w.frames for w in wavs}) > 1
                                     else W.tc_string(w0.frames, w0.sr, self._fps(w0)[0]))
            self.lbl['Samples'].set(MIXED if len(tcs) > 1 else f'{w0.time_reference or 0:,}'.replace(',', '.'))
        self.lbl['Sample rate'].set(self._common(f'{w.sr} Hz' for w in wavs))
        self.lbl['Bit depth'].set(self._common(f'{w.bits} bit' + (' float' if w.eff_tag == 3 else '') for w in wavs))
        self.lbl['Formato'].set(self._common(
            {b'RIFF': 'WAV', b'RF64': 'RF64', b'BW64': 'BW64'}.get(w.kind, '?')
            + ''.join(' ' + c.decode().strip() for c in (b'bext', b'iXML') if c in w.chunk_bodies) for w in wavs))
        self.init = {k: self.v[k].get() for k in self.v}
        self.init['NOTE'] = note
        self._set_editable(True, single)
        self._finish_load()

    def _finish_load(self):
        self.notes.edit_modified(False)
        if not self.edit_ids:
            self.notes.configure(state='disabled')
        self._loading = False
        self._update_dirty()

    def _notes_text(self):
        return self.notes.get('1.0', 'end-1c')

    def _notes_modified(self, _e=None):
        if self.notes.edit_modified():
            self.notes.edit_modified(False)
            self._update_dirty()

    def _fps_changed(self):
        if len(self.edit_ids) != 1 or not self.preserve.get():
            return
        w = self.files[self.edit_ids[0]]
        f, d = fps_from_choice(self.v['FPS'].get())
        tc = W.tc_string(w.time_reference or 0, w.sr, f, d)
        self.v['TC'].set(tc)
        self.init['TC'] = tc           # mismo TimeReference, solo cambia cómo se lee
        self.lbl['Duración'].set(W.tc_string(w.frames, w.sr, f))

    def _preserve_changed(self):
        if self.preserve.get():
            self.v['TC'].set(self.init.get('TC', ''))
            self.w['TC'].configure(state='readonly')
        else:
            if not messagebox.askyesno(
                    APP, 'Cambiar el TC de inicio modifica dónde cae el audio en la línea de tiempo '
                         '(bext TimeReference). Si estaba sincronizado, se pierde la sincro.\n\n¿Habilitar la edición?',
                    icon='warning'):
                self.preserve.set(True)
                return
            self.w['TC'].configure(state='normal')
            self.w['TC'].focus_set()
        self._update_dirty()

    def _edit_track(self, e):
        if len(self.edit_ids) != 1:
            return
        iid = self.tracks.identify_row(e.y)
        col = self.tracks.identify_column(e.x)
        if not iid or col != '#3':
            return
        x, y, wd, ht = self.tracks.bbox(iid, col)
        val = self.tracks.set(iid, 'name')
        ent = ttk.Entry(self.tracks)
        ent.insert(0, val)
        ent.select_range(0, 'end')
        ent.place(x=x, y=y, width=wd, height=ht)
        ent.focus_set()

        def commit(_=None):
            nv = ent.get()
            ent.destroy()
            if nv != val:
                self.tracks.set(iid, 'name', nv)
                il = int(self.tracks.set(iid, 'il') or 0) or (self.tracks.index(iid) + 1)
                self.track_edits[il] = nv
                self._update_dirty()
        ent.bind('<Return>', commit)
        ent.bind('<FocusOut>', commit)
        ent.bind('<Escape>', lambda _: ent.destroy())

    def changes(self):
        if not self.edit_ids or not self.init:
            return {}
        fields = {}
        for k in ENTRY_FIELDS:
            v = self.v[k].get()
            if v != self.init[k] and v != MIXED:
                fields[k] = v.strip()
        for k in CHECK_FIELDS:
            v = self.v[k].get()
            if v != self.init[k] and v in ('TRUE', 'FALSE'):
                fields[k] = v
        note = self._notes_text()
        if note != self.init['NOTE'] and note != MIXED:
            fields['NOTE'] = note
        out = {}
        if fields:
            out['fields'] = fields
        fv = self.v['FPS'].get()
        if fv != self.init['FPS'] and fv in W.FPS_RATES:
            out['fps'] = fv
        if not self.preserve.get() and self.v['TC'].get().strip() != self.init['TC']:
            out['tc'] = self.v['TC'].get().strip()
        if self.track_edits:
            out['tracks'] = dict(self.track_edits)
        return out

    def is_dirty(self):
        return bool(self.changes())

    def _update_dirty(self):
        if self._loading:
            return
        d = self.is_dirty()
        self.dirty_lbl.configure(text='● cambios sin guardar' if d else '')
        for b in (self.btn_save, self.btn_discard):
            b.state(['!disabled'] if d and not self.busy else ['disabled'])

    def discard(self):
        self.load_editor(self.edit_ids)

    def save_meta(self):
        if self.busy:
            return
        ch = self.changes()
        if not ch:
            return
        wavs = [self.files[i] for i in self.edit_ids if i in self.files]
        tr = None
        if 'tc' in ch:
            w = wavs[0]
            if 'fps' in ch:
                f, d = fps_from_choice(ch['fps'])
            else:
                f, d, _ = self._fps(w)
            try:
                tr = W.tc_to_samples(ch['tc'], w.sr, f, d)
            except W.WavError as e:
                messagebox.showerror(APP, str(e))
                return
        jobs, outs = [], []
        mode, cust = self.out_mode.get(), self.out_dir.get()
        for w in wavs:
            od = os.path.dirname(w.path)
            stem, ext = os.path.splitext(os.path.basename(w.path))
            same_dir = os.path.normcase(os.path.abspath(cust or '')) == os.path.normcase(os.path.abspath(od))
            if mode == 'custom' and cust and not same_dir:
                out = os.path.join(cust, stem + ext)
            elif mode == 'sub':
                out = os.path.join(od, 'META', stem + ext)
            else:                      # misma carpeta: mismo nombre + sufijo, nunca pisa el original
                out = os.path.join(od, stem + '_meta' + ext)
            kw = {'fields': ch.get('fields'), 'track_names': ch.get('tracks'),
                  'time_reference': tr, 'fps': W.FPS_RATES[ch['fps']] if 'fps' in ch else None}
            jobs.append({'kind': 'meta', 'wav': w, 'out': out, 'kw': kw, 'weight': w.data_size})
            outs.append(out)
        lines = []
        names = {'SCENE': 'Escena', 'TAKE': 'Toma', 'PROJECT': 'Proyecto', 'TAPE': 'Tape', 'UBITS': 'UBits',
                 'CIRCLED': 'Circulado', 'WILD_TRACK': 'Wild track', 'NOTE': 'Notas'}
        for k, v in (ch.get('fields') or {}).items():
            lines.append(f'  {names[k]}: {v[:60]!r}')
        if 'fps' in ch:
            lines.append(f"  Frame rate: {ch['fps']}")
        if 'tc' in ch:
            lines.append(f"  TC inicio: {ch['tc']}")
        for il, n in (ch.get('tracks') or {}).items():
            lines.append(f'  Track {il}: {n!r}')
        dirs = sorted({os.path.dirname(o) for o in outs})
        if len(outs) == 1:
            dirs = [outs[0]]
        if not messagebox.askyesno(
                APP, f'Guardar una COPIA de {len(wavs)} archivo(s) con estos cambios:\n\n' + '\n'.join(lines)
                + '\n\nLos originales no se tocan. El audio se copia sin cambios.\n\nDestino:\n'
                + '\n'.join(dirs[:6]) + ('\n…' if len(dirs) > 6 else '')):
            return
        self._check_overwrite_and_run(jobs, outs, reset_editor=True)

    # -------------------------------------------------------------- salidas
    def _outdir(self, src_path, sub):
        m = self.out_mode.get()
        if m == 'custom' and self.out_dir.get():
            return self.out_dir.get()
        d = os.path.dirname(src_path)
        return os.path.join(d, sub) if m == 'sub' else d

    def _targets(self):
        sel = self.tree.selection()
        ids = sel if sel else list(self.files.keys())
        return [self.files[i] for i in ids if i in self.files]

    # -------------------------------------------------------------- unir
    def do_merge(self):
        if self.busy:
            return
        monos = [w for w in self._targets() if w.channels == 1]
        if not monos:
            messagebox.showinfo(APP, 'No hay archivos mono para unir (en la selección o en la lista).')
            return
        by = tuple(k for k, v in (('name', self.by_name), ('tc', self.by_tc), ('duration', self.by_dur)) if v.get())
        if not ({'name', 'tc'} & set(by)):
            messagebox.showwarning(APP, 'Activá al menos "Nombre" o "Timecode" para agrupar.')
            return
        if self.cross.get() and 'tc' not in by:
            messagebox.showwarning(APP, 'Para unir monos de carpetas distintas (ej. Tr1/, Tr2/) hay que tildar '
                                        'también "Timecode", así no se mezclan tomas de días distintos.')
            return
        groups = W.find_groups(monos, by, same_dir=not self.cross.get())
        grouped = {w.path for g in groups for w in g['wavs']}
        loose = [w for w in monos if w.path not in grouped]
        if not groups:
            messagebox.showinfo(APP, 'No se encontraron grupos de monos correspondientes.')
            return
        for g in groups:
            ext = os.path.splitext(g['wavs'][0].path)[1]
            g['out'] = os.path.join(self._outdir(os.path.join(g['dir'], '_'), 'POLY'), g['poly_name'] + ext)
        if not self._confirm_merge(groups, loose):
            return
        jobs = [{'kind': 'merge', 'group': g, 'out': g['out'],
                 'weight': g['wavs'][0].data_size * len(g['wavs'])} for g in groups if not g['problems']]
        self._check_overwrite_and_run(jobs, [j['out'] for j in jobs])

    def _confirm_merge(self, groups, loose):
        top = tk.Toplevel(self.root, bg=BG)
        top.title('Confirmar unión')
        top.geometry('900x520')
        top.transient(self.root)
        top.grab_set()
        n_ok = sum(1 for g in groups if not g['problems'])
        pl = lambda n, a, b: f'{n} {a if n == 1 else b}'
        ttk.Label(top, text=pl(n_ok, 'grupo listo', 'grupos listos') + ' para unir'
                  + (f'  ·  {len(groups) - n_ok} con problemas (no se unen)' if n_ok != len(groups) else '')
                  + (f'  ·  {len(loose)} monos sin grupo' if loose else ''),
                  style='Sec.TLabel').pack(anchor='w', padx=16, pady=(14, 8))
        fr = ttk.Frame(top, style='Panel.TFrame')
        fr.pack(fill='both', expand=True, padx=16)
        t = ttk.Treeview(fr, columns=('info', 'state'), show='tree headings')
        t.heading('#0', text='Poly / canales')
        t.heading('info', text='Formato · TC · duración')
        t.heading('state', text='Estado')
        t.column('#0', width=330)
        t.column('info', width=330)
        t.column('state', width=200)
        t.tag_configure('ok', foreground=OK)
        t.tag_configure('err', foreground=ERR)
        t.tag_configure('warn', foreground=WARN)
        vs = ttk.Scrollbar(fr, orient='vertical', command=t.yview)
        t.configure(yscrollcommand=vs.set)
        t.pack(side='left', fill='both', expand=True)
        vs.pack(side='right', fill='y')
        for g in groups:
            w0 = g['wavs'][0]
            fps, drop, _ = self._fps(w0)
            info = (f"{len(g['wavs'])}ch · {w0.sr} Hz · {w0.bits} bit · TC "
                    f"{W.tc_string(w0.time_reference, w0.sr, fps, drop)} · {W.tc_string(w0.frames, w0.sr, fps)}")
            p = t.insert('', 'end', text=os.path.basename(g['out']),
                         values=(info, 'OK' if not g['problems'] else 'NO SE UNE'),
                         tags=('ok' if not g['problems'] else 'err',), open=bool(g['problems']))
            for i, (w, lab) in enumerate(zip(g['wavs'], g['labels']), 1):
                t.insert(p, 'end', text=f'  {i}: {os.path.basename(w.path)}', values=(f'track "{lab}"', ''))
            for pr in g['problems']:
                t.insert(p, 'end', text='  ⚠', values=(pr, ''), tags=('err',))
        if loose:
            p = t.insert('', 'end', text='Monos sin grupo', values=('no coinciden con ningún otro', 'se ignoran'),
                         tags=('warn',))
            for w in loose:
                t.insert(p, 'end', text='  ' + os.path.basename(w.path), values=('', ''))
        res = {'ok': False}
        b = ttk.Frame(top, padding=16)
        b.pack(fill='x')

        def go():
            res['ok'] = True
            top.destroy()
        ttk.Button(b, text='Cancelar', command=top.destroy).pack(side='right')
        bt = ttk.Button(b, text='Unir ' + pl(n_ok, 'grupo', 'grupos'), style='Accent.TButton', command=go)
        bt.pack(side='right', padx=(0, 8))
        if not n_ok:
            bt.state(['disabled'])
        self.root.wait_window(top)
        return res['ok']

    # -------------------------------------------------------------- separar
    def do_split(self):
        if self.busy:
            return
        polys = [w for w in self._targets() if w.channels > 1]
        if not polys:
            messagebox.showinfo(APP, 'No hay archivos poly para separar (en la selección o en la lista).')
            return
        jobs, outs = [], []
        for w in polys:
            stem, ext = os.path.splitext(os.path.basename(w.path))
            od = self._outdir(w.path, 'MONO')
            paths = [os.path.join(od, f'{stem}_{lab}{ext}') for lab in W.split_labels(w, self.split_names.get())]
            jobs.append({'kind': 'split', 'wav': w, 'outs': paths, 'weight': w.data_size})
            outs += paths
        ejemplo = '\n'.join('   ' + os.path.basename(o) for o in jobs[0]['outs'][:8])
        if not messagebox.askyesno(APP, f'¿Separar {len(polys)} poly en {len(outs)} monos?\n\n'
                                        f'Ejemplo:\n{ejemplo}' + ('\n   …' if len(jobs[0]['outs']) > 8 else '')):
            return
        self._check_overwrite_and_run(jobs, outs)

    # -------------------------------------------------------------- ejecución
    def _check_overwrite_and_run(self, jobs, outs, reset_editor=False):
        if not jobs:
            return
        srcs = {os.path.normcase(os.path.abspath(w.path)) for w in self.files.values()}
        if any(os.path.normcase(os.path.abspath(o)) in srcs for o in outs):
            messagebox.showerror(APP, 'Alguna salida pisaría un archivo de origen. Cambiá la carpeta de salida.')
            return
        if len({os.path.normcase(os.path.abspath(o)) for o in outs}) != len(outs):
            messagebox.showerror(APP, 'Dos archivos irían al mismo destino con el mismo nombre. '
                                      'Elegí "Subcarpeta" para que cada uno quede junto a su original.')
            return
        exist = [o for o in outs if os.path.exists(o)]
        if exist and not messagebox.askyesno(
                APP, f'{len(exist)} archivo(s) de salida ya existen:\n\n'
                + '\n'.join(os.path.basename(e) for e in exist[:15])
                + ('\n…' if len(exist) > 15 else '') + '\n\n¿Sobrescribir?', icon='warning'):
            return
        self.busy = True
        self._reset_after = reset_editor
        for b in (self.btn_merge, self.btn_split, self.btn_save, self.btn_discard):
            b.state(['disabled'])
        threading.Thread(target=self._worker, args=(jobs,), daemon=True).start()

    def _worker(self, jobs):
        total = sum(j['weight'] for j in jobs) or 1
        done = 0
        errors, created = [], []
        for j in jobs:
            def prog(d, t, b=done, wgt=j['weight']):
                self.q.put(('prog', (b + wgt * d / max(t, 1)) / total))
            try:
                if j['kind'] == 'merge':
                    g = j['group']
                    self.q.put(('status', f"Uniendo {os.path.basename(j['out'])}…"))
                    os.makedirs(os.path.dirname(j['out']), exist_ok=True)
                    W.merge(g['wavs'], j['out'], g['labels'], progress=prog)
                    created.append(j['out'])
                elif j['kind'] == 'split':
                    self.q.put(('status', f"Separando {os.path.basename(j['wav'].path)}…"))
                    os.makedirs(os.path.dirname(j['outs'][0]), exist_ok=True)
                    W.split(j['wav'], j['outs'], progress=prog)
                    created += j['outs']
                else:
                    self.q.put(('status', f"Escribiendo {os.path.basename(j['out'])}…"))
                    os.makedirs(os.path.dirname(j['out']), exist_ok=True)
                    W.write_meta_copy(j['wav'], j['out'], progress=prog, **j['kw'])
                    created.append(j['out'])
            except Exception as e:           # noqa: BLE001
                name = os.path.basename(j.get('out') or j['wav'].path)
                errors.append(f'{name}: {e}')
            done += j['weight']
        self.q.put(('done', (created, errors)))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == 'status':
                    self.status.set(val)
                elif kind == 'prog':
                    self.prog['value'] = val * 1000
                elif kind == 'done':
                    created, errors = val
                    self.busy = False
                    for b in (self.btn_merge, self.btn_split):
                        b.state(['!disabled'])
                    if not errors:
                        self.prog['value'] = 1000
                    msg = f'Listo: {len(created)} archivo(s) creados.'
                    if errors:
                        msg += f'  {len(errors)} error(es).'
                        messagebox.showerror(APP, 'Errores:\n\n' + '\n'.join(errors[:30]))
                    self.status.set(msg)
                    if self._reset_after and not errors:
                        self.load_editor(self.edit_ids)
                    self._update_dirty()
                    if created:
                        dirs = sorted({os.path.dirname(c) for c in created})
                        if messagebox.askyesno(APP, msg + '\n\nSalida en:\n' + '\n'.join(dirs[:8])
                                               + '\n\n¿Agregar los archivos nuevos a la lista?'):
                            self.load(created)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


def main():
    root = BaseTk()
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                       # noqa: BLE001
        pass
    app = App(root)
    if IS_MAC:                              # archivos soltados sobre el ícono de la app
        try:
            root.createcommand('::tk::mac::OpenDocument', lambda *p: app.load(list(p)))
        except Exception:                   # noqa: BLE001
            pass
    args = [a for a in sys.argv[1:] if not a.startswith(('--', '-psn'))]
    if args:
        root.after(200, lambda: app.load(args))
    root.mainloop()


if __name__ == '__main__':
    main()
