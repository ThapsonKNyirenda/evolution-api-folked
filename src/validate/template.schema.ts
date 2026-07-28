import { JSONSchema7 } from 'json-schema';
import { v4 } from 'uuid';

const isNotEmpty = (...propertyNames: string[]): JSONSchema7 => {
  return {
    allOf: propertyNames.map(property => ({
      if: { required: [property] },
      then: {
        properties: {
          [property]: {
            minLength: 1,
            description: `The "${property}" cannot be empty`,
          },
        },
      },
    })),
  };
};

export const templateSchema: JSONSchema7 = {
  $id: v4(),
  type: 'object',
  properties: {
    name: { type: 'string' },
    category: { type: 'string', enum: ['AUTHENTICATION', 'MARKETING', 'UTILITY'] },
    allowCategoryChange: { type: 'boolean' },
    language: { type: 'string' },
    components: { type: 'array' },
    webhookUrl: { type: 'string' },
  },
  required: ['name', 'category', 'language', 'components'],
  ...isNotEmpty('name', 'category', 'language', 'components'),
};
