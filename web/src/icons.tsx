import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

const base = { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };

export const BookIcon = (props: IconProps) => <svg {...base} {...props}><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H6.5A2.5 2.5 0 0 0 4 21.5z"/><path d="M4 5.5v16M8 7h8M8 11h6"/></svg>;
export const UploadIcon = (props: IconProps) => <svg {...base} {...props}><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"/><path d="M5 14v5h14v-5"/></svg>;
export const SparkIcon = (props: IconProps) => <svg {...base} {...props}><path d="m12 3 1.2 4.1L17 9l-3.8 1.9L12 15l-1.2-4.1L7 9l3.8-1.9z"/><path d="m18.5 14 .6 2.1L21 17l-1.9.9-.6 2.1-.6-2.1L16 17l1.9-.9z"/></svg>;
export const QuoteIcon = (props: IconProps) => <svg {...base} {...props}><path d="M5 11h5v7H4v-5c0-4 2-6.5 5-8M14 11h5v7h-6v-5c0-4 2-6.5 5-8"/></svg>;
export const ArrowIcon = (props: IconProps) => <svg {...base} {...props}><path d="m9 18 6-6-6-6"/></svg>;
export const SendIcon = (props: IconProps) => <svg {...base} {...props}><path d="m22 2-7 20-4-9-9-4zM22 2 11 13"/></svg>;
export const CloseIcon = (props: IconProps) => <svg {...base} {...props}><path d="m6 6 12 12M18 6 6 18"/></svg>;
export const MenuIcon = (props: IconProps) => <svg {...base} {...props}><path d="M4 7h16M4 12h16M4 17h16"/></svg>;
export const CheckIcon = (props: IconProps) => <svg {...base} {...props}><path d="m5 12 4 4L19 6"/></svg>;
export const SettingsIcon = (props: IconProps) => <svg {...base} {...props}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z"/></svg>;
export const PanelIcon = (props: IconProps) => <svg {...base} {...props}><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 4v16M16 4v16"/></svg>;
export const StopIcon = (props: IconProps) => <svg {...base} {...props}><rect x="7" y="7" width="10" height="10" rx="1"/></svg>;
