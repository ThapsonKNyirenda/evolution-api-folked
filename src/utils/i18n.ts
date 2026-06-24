import i18next from 'i18next';
import en from './translations/en.json';

i18next.init({
  resources: {
    en: { translation: en },
  },
  lng: 'en',
  fallbackLng: 'en',
  debug: false,
  interpolation: {
    escapeValue: false,
  },
});
export default i18next;
