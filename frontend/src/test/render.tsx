import { render } from "@testing-library/react";
import type { ReactElement } from "react";

import { LangProvider } from "../i18n";
import type { Lang } from "../i18n";

export const renderIn = (ui: ReactElement, lang: Lang = "en") => render(<LangProvider lang={lang}>{ui}</LangProvider>);
